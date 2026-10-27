/* Quick reply editor, on long press. */
'use strict';
function quickReplyEditor(opts) {
  if (quickEditing) return quickEditing;
  const o = opts || {};
  const defaults = quickClean(Array.isArray(o.defaults) ? o.defaults : QUICK_DEFAULTS);
  let saved = false;
  const shell = modalShell('qr-editor', 'Edit quick replies', () => { quickEditing = null; });
  const list = el('div', { class: 'qr-list' });
  const count = el('span', { class: 'dim qr-count' });
  const err = el('p', { class: 'qr-err bad', role: 'alert' });
  const addBtn = el('button', { type: 'button', class: 'minimal qr-add', onclick: () => { if (rows().length < QUICK_MAX) { const r = addRow(''); sync(); r.querySelector('input').focus(); } }, text: '+ add' });
  const rows = () => Array.from(list.querySelectorAll('.qr-row'));
  const inputs = () => Array.from(list.querySelectorAll('input'));
  const values = () => inputs().map((i) => i.value);
  function clearError() { err.textContent = ''; for (const i of inputs()) i.removeAttribute('aria-invalid'); }
  function sync() {
    const n = rows().length;
    addBtn.disabled = n >= QUICK_MAX;
    if (n >= QUICK_MAX) addBtn.setAttribute('title', 'At most ' + QUICK_MAX + ' replies'); else addBtn.removeAttribute('title');
    count.textContent = n + ' of ' + QUICK_MAX;
  }
  function addRow(text) {
    const input = el('input', { type: 'text', class: 'qr-input', 'aria-label': 'Quick reply', autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false', enterkeyhint: 'done' });
    input.value = text;
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); save(); } });
    input.addEventListener('input', clearError);
    const row = el('div', { class: 'qr-row' }, input,
      el('button', { type: 'button', class: 'icon minimal qr-rm', title: 'Remove', 'aria-label': 'Remove this reply', onclick: () => { row.remove(); sync(); clearError(); } }, ic('cross')));
    list.append(row);
    return row;
  }
  function fill(items) { list.textContent = ''; for (const t of items) addRow(t); sync(); clearError(); }
  /* '' when the rows are fine, else the message for the inline error (and the first offending input) */
  function problem() {
    const ins = inputs();
    if (ins.length > QUICK_MAX) return { msg: 'At most ' + QUICK_MAX + ' replies (this has ' + ins.length + ').', input: ins[QUICK_MAX] };
    const long = ins.find((i) => i.value.trim().length > QUICK_LINE_MAX);
    if (long) { const v = long.value.trim(); return { msg: 'A reply is at most ' + QUICK_LINE_MAX + ' characters ("' + v.slice(0, 24) + '…" has ' + v.length + ').', input: long }; }
    return null;
  }
  function save() {
    if (saved) return;
    const bad = problem();
    if (bad) {
      err.textContent = bad.msg;
      bad.input.setAttribute('aria-invalid', 'true');
      try { bad.input.focus(); } catch (_) { /* nothing to focus */ }
      return;
    }
    saved = true;
    const items = quickClean(values());
    try { if (typeof o.onSave === 'function') o.onSave(items); } catch (e) { console.error('ccboard quickReplyEditor onSave', e); }
    shell.close();
  }
  fill(quickClean(Array.isArray(o.items) ? o.items : defaults));
  shell.dlg.append(el('form', { class: 'qr-box', onsubmit: (e) => { e.preventDefault(); save(); } },
    el('h2', { class: 'qr-title', text: 'Quick replies' }),
    el('p', { class: 'dim qr-hint', text: 'One per row, sent to the session with Enter. Up to ' + QUICK_MAX + ', ' + QUICK_LINE_MAX + ' characters each.' }),
    list, err,
    el('div', { class: 'qr-tools' }, addBtn, el('button', { type: 'button', class: 'minimal qr-reset', title: 'Back to the default replies', onclick: () => fill(defaults), text: 'Reset' }), count),
    el('div', { class: 'qr-actions' },
      el('button', { type: 'button', class: 'qr-cancel', onclick: () => shell.close(), text: 'Cancel' }),
      el('button', { type: 'submit', class: 'primary qr-save', text: 'Save' }))));
  const ctl = { dialog: shell.dlg, close: shell.close, save, values };
  quickEditing = ctl;
  shell.show();
  if (!coarsePointer()) { const first = list.querySelector('input'); if (first) first.focus(); }
  return ctl;
}
