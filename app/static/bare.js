/* Local cached rows only; reading a device is an explicit click, never a timer. */
Shell.bareDevice = function (node, device) {
  node._bareDevice = device;
  const key = 'bare:' + device.ts_id, open = Shell.open.has(key);
  const signature = JSON.stringify([open, device.sessions || []]);
  if (node._bareSignature === signature && node._bareName === device.name) return;
  node._bareSignature = signature; node._bareName = device.name;
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
    const label = 'Stop ' + row.agent + ' ' + row.repo + ' on ' + device.name;
    const stop = el('button', { class: 'icon minimal small tn-add', type: 'button', 'aria-label': label, title: label }, ic('stop'));
    const cancel = el('button', { class: 'minimal small tn-add hidden', type: 'button', text: 'Cancel', hidden: true });
    const disarm = () => {
      armed = false; clearTimeout(timer); stop.textContent = ''; stop.append(ic('stop'));
      stop.classList.add('icon', 'minimal'); stop.classList.remove('danger'); controls.classList.remove('armed');
      cancel.classList.add('hidden'); cancel.setAttribute('hidden', '');
      stop.setAttribute('aria-label', label); stop.setAttribute('title', label); status.textContent = '';
    };
    cancel.addEventListener('click', disarm);
    const controls = el('span', { class: 'bare-controls' }, stop, cancel);
    stop.disabled = row.state === 'stopping';
    stop.addEventListener('click', async () => {
      if (!armed) {
        armed = true; stop.textContent = 'Confirm'; stop.classList.remove('icon', 'minimal'); stop.classList.add('danger'); controls.classList.add('armed');
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
      el('span', { class: 'tn-sub', text: row.state || 'unknown' }), controls));
  }
};
