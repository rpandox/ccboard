/* Split for first paint (#333); loaded by lazy.js on use. */
'use strict';

/* Install now (#313): the button reads "installing at the next check" and is off at the click; a poll in flight cannot flip it back (ui.installNow, 15 s), a refusal restores it. */
async function deployNow() {
  ui.installNow = Date.now() + 15000;
  renderBanner();
  try { await api('POST', '/api/deploy/now'); } catch (e) {
    ui.installNow = 0;
    renderBanner();
    toast(`The update was not started: ${errorText(e)}`, { kind: 'bad' });
  }
  await poll(true);
}

async function decide(pid, decision) {
  if (actPending.has('perm:' + pid)) return false;               // #318: an answer is already out (the keys y / d are not guarded by a disabled button): never post a second one
  const pr = (state && state.pending_permissions || []).find((p) => p.id === pid);
  const bare = pr && pr.bare_device;
  const ok = await actPost('perm:' + pid, decision, bare ? `/api/bare/${encodeURIComponent(bare)}/sessions/${encodeURIComponent(pr.tmux_name)}/${decision}` : `/api/permission/${pid}/${decision}`, bare ? { pid } : undefined,
    () => repaintPage(), `${decision === 'allow' ? 'Allow' : 'Deny'} was not sent`);     // #313: both buttons off and "allowing…" before the board answers
  if (ok) setError(null);
  await poll(true);
  return ok;
}

const INBOX_HINT = 'j / k move · Enter open · o terminal · a ack · y allow · d deny · r reply · ? all keys';

/* The v0.4 board's section renderers are gone; these two names stay because confirmButton() (components.js) and the launcher forms call them
   after a local change, and the keyboard layer's tests call renderInbox(). Both just repaint whatever page is mounted. */
function renderInbox() { repaintPage(); }

function renderProjects() { repaintPage(); }


async function logout() {
  try { const r = await api('POST', '/api/claude/logout'); if (!r.ok) setError('logout: ' + (r.output || 'failed')); else setError(null); }
  catch (e) { setError(e.message); }
  await poll(true);
}

