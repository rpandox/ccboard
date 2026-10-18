/* Cached rows from the server poller; the browser never schedules device reads. */
Shell.bareDevice = function (node, device) {
  node._bareDevice = device;
  const key = 'bare:' + device.ts_id, open = Shell.open.has(key);
  const signature = JSON.stringify([open, device.sessions || []]);
  if (node._bareSignature === signature && node._bareName === device.name) return;
  /* A rebuild would drop the caret in a Reply box and disarm a Stop. Keep the old rows until the person lets go; the next patch repaints. */
  const focused = typeof document !== 'undefined' && document.activeElement;
  const typing = focused && node.contains(focused) && /^(TEXTAREA|INPUT)$/i.test(focused.tagName || '');
  if (node._bareSignature && node._bareOpen === open && (typing || (node._bareArmed && node._bareArmed.size))) return;
  node._bareSignature = signature; node._bareName = device.name; node._bareOpen = open; node._bareArmed = new Set();
  node.textContent = '';
  node.classList.add('tn', 'bare-tree');
  node.classList.toggle('open', open);
  node.setAttribute('data-key', key);
  const status = el('span', { class: 'bare-status dim', role: 'status' });
  const readLabel = 'Refresh sessions on ' + device.name;
  const read = el('button', { class: 'icon minimal small tn-add', type: 'button', 'aria-label': readLabel, title: readLabel }, ic('refresh'));
  read.addEventListener('click', async () => {
    read.disabled = true;
    try {
      const r = await api('POST', '/api/bare/' + encodeURIComponent(device.ts_id) + '/sessions/read', {});
      node._bareDevice.sessions = r.sessions; Shell.patchTrees();
    } catch (_) { status.textContent = 'No reading; sessions may still run.'; }
    finally { read.disabled = false; }
  });
  node.append(el('div', { class: 'tn-row', role: 'treeitem', tabindex: '-1', 'aria-level': '1', 'aria-expanded': String(open) },
    el('span', { class: 'tw' }, ic('chevron-right')), el('span', { class: 'tn-name', text: device.name }), el('span', { class: 'bare-badge', text: 'no board' }), read), status);
  if (!open) return;
  const kids = el('div', { class: 'tn-kids', role: 'group' });
  node.append(kids);
  for (const row of device.sessions || []) {
    let armed = false, timer = null;
    node._bareArmed ||= new Set();
    const label = 'Stop ' + row.agent + ' ' + row.repo + ' on ' + device.name;
    const stop = el('button', { class: 'icon minimal small tn-add', type: 'button', 'aria-label': label, title: label }, ic('stop'));
    const cancel = el('button', { class: 'minimal small tn-add hidden', type: 'button', text: 'Cancel', hidden: true });
    const disarm = () => {
      armed = false; clearTimeout(timer); node._bareArmed.delete(row.tmux_name); stop.textContent = ''; stop.append(ic('stop'));
      stop.classList.add('icon', 'minimal'); stop.classList.remove('danger'); controls.classList.remove('armed');
      cancel.classList.add('hidden'); cancel.setAttribute('hidden', '');
      stop.setAttribute('aria-label', label); stop.setAttribute('title', label); status.textContent = '';
    };
    cancel.addEventListener('click', disarm);
    const terminal = el('button', { class: 'icon minimal small tn-add', type: 'button',
      'aria-label': 'Open terminal for ' + row.agent + ' ' + row.repo + ' on ' + device.name,
      title: 'Open terminal for ' + row.agent + ' ' + row.repo + ' on ' + device.name }, ic('console'));
    terminal.addEventListener('click', async () => {
      terminal.disabled = true;
      try {
        const r = await api('POST', '/api/bare/' + encodeURIComponent(device.ts_id) + '/sessions/' + encodeURIComponent(row.tmux_name) + '/attach', {});
        location.assign(r.attach_url + (demoOn() ? '?demo=1' : ''));
      } catch (_) { status.textContent = 'Could not open the terminal. Check the device and try again.'; }
      finally { terminal.disabled = false; }
    });
    node._bareDrafts ||= {};
    const draft = node._bareDrafts[row.tmux_name] ||= { text: '', open: false };
    const input = el('textarea', { 'aria-label': 'Reply to ' + row.agent + ' on ' + device.name, placeholder: 'Reply' });
    input.value = draft.text;
    input.addEventListener('input', () => { draft.text = input.value; });
    const send = el('button', { type: 'submit', class: 'small', text: 'Send' });
    const form = el('form', { class: 'bare-reply' + (draft.open ? '' : ' hidden') }, input, send);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!input.value.trim() || send.disabled) return;
      send.disabled = true;
      const text = input.value;
      input.value = ''; draft.text = ''; status.textContent = 'Sending…';          // #311: the box empties at once; a refusal puts the text back
      try {
        await api('POST', '/api/bare/' + encodeURIComponent(device.ts_id) + '/sessions/' + encodeURIComponent(row.tmux_name) + '/reply', { text });
        status.textContent = 'Reply sent.';
      } catch (e) {
        if (!input.value) { input.value = text; draft.text = text; }
        status.textContent = typeof errorText === 'function' ? errorText(e) : e.message;
      }
      finally { send.disabled = false; }
    });
    /* The server takes a reply only at the input prompt, so only a waiting row gets the button and the box; a draft is kept for when it waits again. */
    const waiting = row.state === 'waiting';
    const replyLabel = 'Reply to ' + row.agent + ' ' + row.repo + ' on ' + device.name;
    const reply = waiting ? el('button', { type: 'button', class: 'icon minimal small tn-add bare-replybtn', 'aria-label': replyLabel, title: replyLabel, 'aria-expanded': String(draft.open) }, ic('comment')) : null;
    if (reply) reply.addEventListener('click', () => {
      draft.open = !draft.open; form.classList.toggle('hidden', !draft.open);
      reply.setAttribute('aria-expanded', String(draft.open)); if (draft.open) input.focus();
    });
    const controls = el('span', { class: 'bare-controls' }, terminal, reply, stop, cancel);
    stop.disabled = row.state === 'stopping';
    stop.addEventListener('click', async () => {
      if (!armed) {
        armed = true; node._bareArmed.add(row.tmux_name); stop.textContent = 'Confirm'; stop.classList.remove('icon', 'minimal'); stop.classList.add('danger'); controls.classList.add('armed');
        cancel.classList.remove('hidden'); cancel.removeAttribute('hidden');
        stop.setAttribute('aria-label', 'Confirm stop ' + row.agent + ' ' + row.repo + ' on ' + device.name);
        stop.setAttribute('title', stop.getAttribute('aria-label'));
        status.textContent = 'Tap Confirm to stop, or Cancel.';
        timer = setTimeout(disarm, 5000);
        return;
      }
      disarm();
      stop.disabled = true;
      try {
        await api('POST', '/api/bare/' + encodeURIComponent(device.ts_id) + '/sessions/' + encodeURIComponent(row.tmux_name) + '/stop', { confirm: true });
        const current = (node._bareDevice.sessions || []).find((s) => s.tmux_name === row.tmux_name);
        if (current) current.state = 'stopping';
        Shell.patchTrees();
      } catch (_) { status.textContent = 'Stop was not confirmed. Read the device.'; read.disabled = false; stop.disabled = false; }
    });
    kids.append(el('div', { class: 'tn-row s-row', role: 'treeitem', tabindex: '-1', 'aria-level': '2' },
      el('span', { class: 'tn-name', text: row.agent + ' · ' + row.repo, title: row.agent + ' · ' + row.repo }),
      el('span', { class: 'tn-sub', text: row.state === 'dialog' ? 'needs you' : row.state || 'unknown', title: row.last_event === 'bare_log' ? 'State from the device state log' : 'State read from the screen' }), controls));
    if (waiting) kids.append(form);
  }
};
