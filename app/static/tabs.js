/* Tab strips used by Project, Settings and Memory. */
'use strict';

/* tabs(items:[{id,label,count?}], activeId, onChange) -> { root, set(id), setCount(id, n), value }. Only the tab list: the page renders the panel.
   set() repaints without calling onChange (so a route change cannot loop); a click or an arrow key calls onChange(id). */
let tabsSeq = 0;
function tabs(items, activeId, onChange) {
  const seq = ++tabsSeq;
  let shared = null;                                                       // link(node): ONE panel whose content follows the selected tab
  const list = el('div', { class: 'tablist', role: 'tablist' });
  const root = el('div', { class: 'tabs' }, list);
  const nodes = new Map();
  const counts = new Map();
  const ids = items.map((it) => it.id);
  let current = ids.includes(activeId) ? activeId : ids[0];
  // the tab list scrolls sideways when the tabs do not fit (390 px, six tabs): bring the selected one into view once the list is in the page
  const reveal = (n) => {
    if (typeof requestAnimationFrame !== 'function') return;
    requestAnimationFrame(() => {
      try {
        const a = n.getBoundingClientRect();
        const b = list.getBoundingClientRect();
        if (a.left < b.left) list.scrollLeft -= b.left - a.left;
        else if (a.right > b.right) list.scrollLeft += a.right - b.right;
      } catch (_) { /* no layout */ }
    });
  };
  const paint = (id) => {
    current = id;
    for (const [tid, n] of nodes) {
      n.setAttribute('aria-selected', tid === id ? 'true' : 'false');
      n.setAttribute('tabindex', tid === id ? '0' : '-1');
    }
    if (shared && nodes.has(id)) shared.setAttribute('aria-labelledby', nodes.get(id).getAttribute('id'));
    if (nodes.has(id)) reveal(nodes.get(id));
  };
  const pick = (id, focus) => {
    if (id !== current) { paint(id); if (typeof onChange === 'function') onChange(id); }
    if (focus) nodes.get(id).focus();
  };
  for (const it of items) {
    const count = it.count === undefined || it.count === null ? null : el('span', { class: 'tab-count', text: String(it.count) });
    if (count) counts.set(it.id, count);
    const n = el('div', { class: 'tab', role: 'tab', id: `tab${seq}-${it.id}`, 'data-tab': it.id, onclick: () => pick(it.id, false), onkeydown: (e) => {
      const i = ids.indexOf(it.id);
      let to = null;
      if (e.key === 'ArrowRight') to = ids[(i + 1) % ids.length];
      else if (e.key === 'ArrowLeft') to = ids[(i + ids.length - 1) % ids.length];
      else if (e.key === 'Home') to = ids[0];
      else if (e.key === 'End') to = ids[ids.length - 1];
      else if (e.key === 'Enter' || e.key === ' ') to = it.id;
      if (to === null) return;
      e.preventDefault();
      pick(to, true);
    } }, it.label, count);
    nodes.set(it.id, n);
    list.append(n);
  }
  paint(current);
  return {
    root,
    set(id) { if (nodes.has(id)) paint(id); },
    setCount(id, n) { const c = counts.get(id); if (c) setText(c, n); },
    get value() { return current; },
    /* Tie the tabs to their panels for assistive tech: link({tabId: panelNode}) gives every tab aria-controls and every panel an id and aria-labelledby; link(node) does it for ONE panel
       that shows whichever tab is selected (its aria-labelledby follows). Only panels that exist are referenced: an aria-controls to nothing is an error. */
    link(panels) {
      const one = panels && panels.nodeType === 1 ? panels : null;
      for (const [tid, n] of nodes) {
        const panel = one || (panels && panels[tid]);
        if (!panel || typeof panel.setAttribute !== 'function') continue;
        if (!panel.getAttribute('id')) panel.setAttribute('id', one ? `tabpanel${seq}` : `tabpanel${seq}-${tid}`);
        panel.setAttribute('role', 'tabpanel');
        n.setAttribute('aria-controls', panel.getAttribute('id'));
        if (!one) panel.setAttribute('aria-labelledby', n.getAttribute('id'));
      }
      if (one) { shared = one; one.setAttribute('aria-labelledby', nodes.get(current).getAttribute('id')); }
    },
  };
}

/* openSheet({title, body, actions?, placement?, wide?, onClose?, back?}) -> { dialog, body, close }. dialog#sheet through showModal(): a right panel from 840 px up,
   a bottom sheet below (placement 'right' | 'bottom' forces one). Calling it again while open swaps the content in place. body and actions
   take a node, an array of nodes or a string. Esc, the backdrop and the close button end it; closeSheet() does the same from code.
   back {label, onClick} puts a chevron button before the title (the way up one level: the repo picker behind a form). The dialog itself takes
   the focus on open, so the browser does not ring the close button; a form's focusFirst (focusFine) then moves it on to the first field, on a
   fine pointer only. */
