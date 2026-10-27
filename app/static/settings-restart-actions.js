/* Restart readiness, request and polling, loaded on first interaction. */
'use strict';

async function settingsRestartArm() {
  const m = settingsRestart;
  m.force = false;
  settingsRestartPaint();
  if (ui.confirm !== 'settings-restart' || (typeof demoOn === 'function' && demoOn())) return;
  try {
    const data = await api('GET', '/api/settings/options?readiness=1');
    if (ui.confirm === 'settings-restart' && !m.sending && !m.active && !m.force && data.restart) {
      m.info = data.restart; settingsRestartPaint();
    }
  } catch (_) { /* Confirm checks readiness again on the server before accepting the restart. */ }
}

async function settingsRestartSend() {
  const m = settingsRestart;
  if (m.active || m.sending) return;
  m.sending = true; m.message = 'Requesting restart…'; settingsRestartPaint();
  const demo = typeof demoOn === 'function' && demoOn();
  let deadline;
  try {
    const answer = demo ? { boot_id: state.boot?.id || 'demo', expect_seconds: 2 } : await Promise.race([
      api('POST', '/api/settings/restart', { force: m.force }),
      new Promise((_, reject) => { deadline = setTimeout(() => reject(new Error('No response')), 10000); }),
    ]);
    m.boot = answer.boot_id; m.seconds = answer.expect_seconds; m.at = Date.now();
    m.active = true; m.message = ''; m.force = false;
    pageToast('Restarting board', 'ok');
    m.timer = setTimeout(() => settingsRestartPoll(demo), 1000);
  } catch (e) {
    const body = e.body || {};
    if (body.reason === 'busy') {
      m.info.blockers = body.blockers || []; m.force = true; ui.confirm = 'settings-restart';
      m.message = 'Review these warnings, then Confirm Restart board to continue, or Cancel.';
    } else {
      m.message = body.reason === 'rate' ? (body.message || `Wait ${body.retry_after || 60} seconds, then try again.`) : 'Restart was not confirmed. Check the commands below, or try again.';
      if (body.command || body.hint) m.info.hint = body.command || body.hint;
    }
  } finally { clearTimeout(deadline); m.sending = false; settingsRestartPaint(); }
}

async function settingsRestartPoll(demo = false) {
  const m = settingsRestart;
  clearTimeout(m.timer);
  if (!m.active) return;
  if (Date.now() - m.at >= 90000) {
    m.active = false; m.message = `No new board process after 90 seconds. Check ${m.info?.launch_log || 'the logs'} and run the restart command, or try again.`;
    settingsRestartPaint(); return;
  }
  let deadline;
  try {
    const next = demo ? { ...state, boot: { id: 'demo-' + Date.now() }, settings_pending: { count: 0 } } : await Promise.race([
      api('GET', '/api/state'), new Promise((_, reject) => { deadline = setTimeout(() => reject(new Error('offline')), 4000); }),
    ]);
    if (next.boot?.id && next.boot.id !== m.boot) {
      m.active = false; m.message = ''; m.info.blockers = []; state = next;
      if (demo) {
        m.demoBoot = next.boot;
        if (!settingsDemoOptions) settingsDemoOptions = m.options || await demoApi('GET', '/api/settings/options');
        settingsDemoOptions.pending = [];
        for (const row of settingsDemoOptions.options) { row.running = row.value; row.pending = false; }
      }
      updateCurrentPage(state); settingsRestartPaint(); pageToast('Board restarted', 'ok');
      return;
    }
  } catch (e) { m.message = e.status === 401 || e.status === 403 ? 'Sign in again with bin/ccboard-open, then reload this page.' : 'Offline, retrying.'; }
  finally { clearTimeout(deadline); }
  settingsRestartPaint();
  m.timer = setTimeout(() => settingsRestartPoll(demo), 1000);
}

