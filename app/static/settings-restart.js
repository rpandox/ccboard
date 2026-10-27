/* Restart notice shared by Home and Settings; action code loads on the first tap. */
'use strict';
let settingsDemoOptions = null;

function settingsRestartCommand(command) {
  return command ? el('div', { class: 'doc-cmdline' }, el('code', { class: 'doc-cmd', text: command }), copyButton(command, 'the restart command')) : null;
}

const settingsRestart = { info: null, active: false, sending: false, force: false, message: '', at: 0, boot: null, timer: null };

function settingsRestartPaint() {
  document.querySelectorAll('.set-restart-control').forEach(root => settingsRestartFill(root));
}

function settingsRestartFill(root) {
  const m = settingsRestart, info = m.info || {};
  root.textContent = '';
  if (!info.method) return;
  if (info.method === 'none') {
    root.append(el('p', { text: info.why }), settingsRestartCommand(info.hint));
    return;
  }
  if (!m.active && !m.sending) {
    const control = confirmButton('settings-restart', 'Restart board', settingsRestartSend, false, settingsRestartArm);
    root.append(control);
  }
  for (const line of info.blockers || []) root.append(el('p', { class: 'set-restart-blocker', text: line }));
  if (m.message) root.append(el('p', { role: 'status', text: m.message }));
  if (m.active) root.append(el('p', { role: 'status', text: `Restarting. Back in about ${m.seconds} seconds. ${Math.floor((Date.now() - m.at) / 1000)} seconds since the tap.` }));
  if (m.message && !m.active) root.append(settingsRestartCommand(info.logs), settingsRestartCommand(info.hint));
}

function settingsRestartControl(info) {
  if (info) settingsRestart.info = info;
  const root = el('div', { class: 'set-restart-control' });
  settingsRestartFill(root);
  return root;
}

function settingsRestartDemoState(st) {
  return settingsRestart.demoBoot ? { ...st, boot: settingsRestart.demoBoot, settings_pending: { count: 0 } } : st;
}

function settingsRestartBanner(pending, home = false) {
  const labels = pending.labels || [];
  const names = labels.slice(0, 3).join(', ') + (pending.count > 3 ? ' and ' + (pending.count - 3) + ' more' : '');
  return el('div', { class: 'callout warn settings-restart ' + (home ? 'home-settings-pending' : 'set-pending'), role: 'status' },
    el('p', { text: (pending.count === 1 ? 'A saved setting waits' : 'Saved settings wait') + ' for a restart' + (names ? ': ' + names : '') + '.' }),
    settingsRestartCommand(pending.restart_hint),
    pending.restart?.docker_notice ? el('p', { text: pending.restart.docker_notice }) : null,
    settingsRestartControl(pending.restart),
    home ? el('a', { href: '#/settings?sec=options', text: 'Open Settings > Options' }) : null);
}

function settingsHomePending(r, pending) {
  pending = { ...pending, restart: state.restart };
  const sig = JSON.stringify(pending);
  if (r.settingsPendingSig === sig) return;
  r.settingsPendingSig = sig;
  if (r.settingsPending) r.settingsPending.remove();
  r.settingsPending = pending.count ? settingsRestartBanner(pending, true) : null;
  if (r.settingsPending) r.summary.parentNode.insertBefore(r.settingsPending, r.summary);
}

