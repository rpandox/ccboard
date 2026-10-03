/* ccboard terminal page (v0.5.8): a ttyd iframe made usable on a phone. The header says which session this is (state, model, context) and has the
   text-size buttons; a collapsible strip shows the task and the last prompt; the right-edge rail, the History chip and the key bar scroll and drive
   the pane through POST /api/sessions/<name>/scroll and /keys; a touch swipe on the terminal becomes mouse-wheel events (TermKit.bind); the composer
   (components.js: Enter sends, Shift+Enter or the newline key adds a line) sits above the soft keyboard because body.term is sized by --vvh.
   The session and its pane are polled together every 3 s (paused while the page is hidden). Loaded after core.js, components.js and termkit.js,
   which provide el(), api(), toast(), the composer helpers and TermKit; this file starts everything. */
'use strict';

(function termPage() {
  let NAME = '';
  try { NAME = decodeURIComponent(location.pathname.replace(/^\/term\//, '').replace(/\/$/, '')); } catch (_) { NAME = ''; }
  const ENC = encodeURIComponent(NAME);
  const API = '/api/sessions/' + ENC;
  const INTERNAL = NAME.startsWith('_ccboard');                       // the login session: no session row, no polling
  const PARTS = NAME.split('--');
  const KEY_FS = 'ccboard:term:fs';
  const KEY_CTX = 'ccboard:term:ctx';
  const KEY_KEYS = 'ccboard:term:keys';
  const QUICK_KEY = 'ccboard:quick:' + NAME;
  const DEFAULT_QUICK = ['y', 'yes', 'continue', 'n', 'run the tests', '/compact'];
  const POLL_MS = 3000;

  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (_) { return null; } },
    set(k, v) { try { if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch (_) { /* storage may be unavailable */ } },
  };

  /* A tap on one of our buttons must not take focus from the composer (the soft keyboard would close). pointerdown is cancelled, click still fires. */
  function keep(b) { b.addEventListener('pointerdown', (e) => e.preventDefault()); return b; }

  /* The same error text can repeat at the speed of a held key: show it once per 3 s. */
  let lastNote = { text: '', at: 0 };
  function note(text, kind) {
    const now = Date.now();
    if (text === lastNote.text && now - lastNote.at < 3000) return;
    lastNote = { text, at: now };
    toast(text, { kind: kind || 'bad' });
  }

  /* ---------- sending ---------- */

  async function postKeys(body) {
    try { await api('POST', API + '/keys', body); } catch (e) { note(e.message); return false; }
    pollSoon(500);
    return true;
  }
  const sendKeys = (keys) => postKeys({ keys });
  const sendText = (text, enter) => postKeys({ text, enter: !!enter });

  async function scroll(dir) {
    try { await api('POST', API + '/scroll', { dir, n: 1 }); } catch (e) { note(e.message); }
    pollSoon(150);
  }

  /* ---------- header ---------- */

  const head = {
    sess: el('span', { class: 'name', text: INTERNAL ? 'login' : (PARTS.length === 3 ? PARTS[2] : NAME) }),
    glyph: el('span'), state: el('span'), meta: el('div', { class: 'l2' }),
    agent: null, stateSig: '', metaSig: '',
  };
  head.meta.textContent = INTERNAL ? 'terminal' : (PARTS.length === 3 ? projectLabel(PARTS[0], PARTS[1]) : '');
  $('#headid').append(el('div', { class: 'l1' }, head.sess, head.glyph, head.state), head.meta);

  function projectLabel(project, repo) { return repo === 'root' ? project : project + '/' + repo; }

  function setState(node) {
    head.state.textContent = '';
    if (node) head.state.append(node);
  }

  function renderHeader(s) {
    setText(head.sess, s.name || (PARTS.length === 3 ? PARTS[2] : NAME));
    const agent = s.agent || 'shell';
    if (head.agent !== agent) { head.agent = agent; head.glyph.textContent = ''; head.glyph.append(agentGlyph(agent)); }
    const badge = stateBadge(s);
    const sig = badge ? badge.textContent + '|' + (s.state || '') : '';
    if (sig !== head.stateSig) { head.stateSig = sig; setState(badge); }
    const stats = s.stats || {};
    const pct = typeof stats.context_pct === 'number' ? Math.round(stats.context_pct) : null;
    const metaSig = [s.project, s.repo, stats.model, pct].join('|');
    if (metaSig !== head.metaSig) {
      head.metaSig = metaSig;
      head.meta.textContent = '';
      const bits = [];
      if (s.project) bits.push(el('span', { text: projectLabel(s.project, s.repo || 'root') }));
      if (stats.model) bits.push(el('span', { text: stats.model }));
      if (pct !== null) bits.push(el('span', { class: pct >= 90 ? 'bad' : (pct >= 80 ? 'warn' : ''), text: 'ctx ' + pct + '%' }));
      bits.forEach((b, i) => { if (i) head.meta.append(' · '); head.meta.append(b); });
    }
    document.title = (s.name || head.sess.textContent) + ' · ccboard';
  }

  function renderGone(text) {
    head.stateSig = 'gone:' + text;
    setState(el('span', { class: 'state ended', text }));
  }

  /* ---------- font size ---------- */

  const storedFs = TermKit.clampFont(store.get(KEY_FS));
  function bumpFont(delta) {
    const t = bound.term();
    const cur = t && t.options && Number(t.options.fontSize) ? Number(t.options.fontSize) : (storedFs || 13);
    const next = TermKit.clampFont(cur + delta);
    if (next === null) return;
    if (bound.setFontSize(next) === null) { note('The terminal is still loading', 'warn'); return; }
    store.set(KEY_FS, String(next));
  }

  /* ---------- context strip ---------- */

  const strip = $('#ctxstrip');
  let ctxOn = store.get(KEY_CTX) !== '0';
  let ctxSig = '';
  let ctxHas = false;
  const ctxBtn = keep(el('button', { class: 'icon minimal', type: 'button', 'aria-label': 'Show or hide the task and prompt', title: 'Task and prompt', onclick: () => {
    ctxOn = !ctxOn;
    store.set(KEY_CTX, ctxOn ? '1' : '0');
    syncCtx();
  } }));

  function syncCtx() {
    ctxBtn.textContent = '';
    ctxBtn.append(ic(ctxOn ? 'chevron-up' : 'chevron-down'));
    ctxBtn.setAttribute('aria-expanded', ctxOn ? 'true' : 'false');
    strip.classList.toggle('hidden', !(ctxOn && ctxHas));
  }

  function link(text, href) {
    return el('a', { class: 'cx-link', href, text, onclick: (e) => { e.preventDefault(); e.stopPropagation(); location.assign(href); } });
  }

  function renderStrip(s) {
    const p = TermKit.contextParts(s);
    ctxHas = !!(p.task || p.prompt || p.ask);
    const sig = JSON.stringify(p);
    if (sig !== ctxSig) {
      ctxSig = sig;
      strip.textContent = '';
      if (p.task) strip.append(el('div', { class: 'cx-task' }, p.task, p.phase ? el('span', { class: 'badge', text: p.phase }) : null));
      if (p.prompt) strip.append(el('div', { class: 'cx-prompt', text: '› ' + p.prompt }));
      if (p.ask) strip.append(el('div', { class: 'cx-ask ' + p.askKind, text: (p.askKind === 'message' ? '' : '? ') + p.ask }));
      if (ctxHas && !INTERNAL) {
        strip.append(el('div', { class: 'cx-links' }, link('Open in board', '/#/s/' + ENC), p.taskId !== null ? link('Tasks', '/#/tasks') : null));
      }
    }
    syncCtx();
  }
  strip.addEventListener('click', () => strip.classList.toggle('open'));

  /* ---------- scroll rail and the History chip ---------- */

  const wrap = $('#ttywrap');
  const chip = $('#histchip');
  chip.textContent = '';
  chip.append('History', el('span', { class: 'dim', text: 'tap for live' }));
  TermKit.pressable(chip, () => scroll('exit'));
  chip.removeAttribute('tabindex');

  function railButton(icon, label, dir, repeat) {
    const b = el('button', { type: 'button', class: 'rail-btn', 'aria-label': label, title: label }, ic(icon));
    TermKit.pressable(b, () => scroll(dir), repeat ? { repeat: true, every: 150, backlog: false } : {});
    return b;
  }
  $('#rail').append(
    railButton('chevron-up', 'Page up (scroll back)', 'up', true),
    railButton('chevron-down', 'Page down (scroll forward)', 'down', true),
    railButton('double-chevron-up', 'Scroll to the top', 'top', false),
    railButton('double-chevron-down', 'Scroll to the bottom', 'bottom', false));

  function applyPane(p) {
    const inMode = !!(p && p.in_mode);
    chip.classList.toggle('hidden', !inMode);
    if (inMode) chip.setAttribute('aria-hidden', 'false'); else chip.setAttribute('aria-hidden', 'true');
  }

  /* ---------- key bar, key mode and the soft keyboard ---------- */

  const kb = TermKit.keyBar($('#keyhost'), { send: sendKeys, sendText, scroll, compact: false });
  let lastCompact = false;
  let baseH = 0;
  let baseW = 0;

  function keysOverride() { const v = store.get(KEY_KEYS); return v === 'compact' || v === 'full' ? v : null; }

  function isCoarse() {
    try { return document.documentElement.classList.contains('force-coarse') || window.matchMedia('(pointer:coarse)').matches; } catch (_) { return false; }
  }

  const keyModeBtn = keep(el('button', { type: 'button', class: 'minimal', title: 'Key bar: auto, compact or full', onclick: () => {
    const cur = keysOverride();
    const next = cur === null ? 'compact' : (cur === 'compact' ? 'full' : null);
    store.set(KEY_KEYS, next);
    applyLayout();
  } }));

  /* Keyboard up = the visual viewport lost 30 % of the window. Android Chrome (interactive-widget=resizes-content) shrinks the window itself, so
     the reference height is the largest one seen at this width, not innerHeight alone. */
  function applyLayout() {
    const vv = window.visualViewport;
    const h = vv ? vv.height : window.innerHeight;
    if (window.innerWidth !== baseW) { baseW = window.innerWidth; baseH = 0; }
    baseH = Math.max(baseH, window.innerHeight, h);
    const kbd = isCoarse() && TermKit.compactKeys(null, h, baseH);
    const short = h < 500;                                         // a phone in landscape, or a squat window: same squeeze as a soft keyboard
    document.body.classList.toggle('kbd', kbd);
    document.body.classList.toggle('short', short);
    const mode = keysOverride();
    const compact = mode ? mode === 'compact' : (kbd || short);
    if (compact !== lastCompact) { lastCompact = compact; kb.setCompact(compact); }
    setText(keyModeBtn, 'keys: ' + (mode || 'auto'));
  }

  /* ---------- quick replies ---------- */

  function loadQuick() { try { const v = JSON.parse(store.get(QUICK_KEY) || 'null'); return Array.isArray(v) ? v : DEFAULT_QUICK; } catch (_) { return DEFAULT_QUICK; } }

  function renderQuick() {
    const q = $('#quick');
    q.textContent = '';
    for (const t of loadQuick()) q.append(keep(el('button', { type: 'button', text: t, onclick: () => sendText(t, true) })));
  }

  function editQuick() {
    const v = window.prompt('Quick replies, one per line (sent with Enter):', loadQuick().join('\n'));
    if (v === null) return;
    store.set(QUICK_KEY, JSON.stringify(v.split('\n').map((x) => x.trim()).filter(Boolean).slice(0, 12)));
    renderQuick();
  }

  $('#qtools').append(keyModeBtn, keep(el('button', { type: 'button', class: 'icon minimal', title: 'Edit quick replies', 'aria-label': 'Edit quick replies', onclick: editQuick }, ic('edit'))));

  /* ---------- composer ---------- */

  /* Enter sends, Shift+Enter or the newline key adds a line (components.js composer). Multi-line text is pasted into the pane as one bracketed
     paste, so Claude Code keeps the newlines inside the prompt. An empty box plus Enter forwards a bare Enter. */
  const sendBox = $('#sendtext');
  function submitSend() {
    const v = sendBox.value.replace(/\r\n?/g, '\n');
    if (v.trim()) { sendText(v, true); sendBox.value = ''; composerGrow(sendBox); } else { sendKeys(['Enter']); }
  }
  composerBind(sendBox, { onSend: submitSend });
  $('#sendform').addEventListener('submit', (e) => { e.preventDefault(); submitSend(); });
  keep($('#nl')).addEventListener('click', () => composerInsertNewline(sendBox));
  keep($('#sendbtn'));
  sendBox.addEventListener('focus', () => wrap.classList.remove('active'));

  /* ---------- header buttons, back ---------- */

  $('#headtools').append(
    keep(el('button', { class: 'minimal fs', type: 'button', 'aria-label': 'Smaller text', title: 'Smaller text', onclick: () => bumpFont(-1) }, 'A−')),
    keep(el('button', { class: 'minimal fs', type: 'button', 'aria-label': 'Larger text', title: 'Larger text', onclick: () => bumpFont(1) }, 'A+')),
    ctxBtn);

  $('#back').addEventListener('click', (e) => {
    e.preventDefault();
    if (TermKit.backTarget(document.referrer, location.origin) !== 'back') { location.assign('/#/agents'); return; }
    history.back();
    const t = setTimeout(() => { location.assign('/#/agents'); }, 600);            // history.back() with nothing behind it does not navigate
    window.addEventListener('pagehide', () => clearTimeout(t), { once: true });
  });

  /* ---------- the iframe ---------- */

  const frame = $('#tty');
  const bound = TermKit.bind(frame, {
    onActive: () => wrap.classList.add('active'),
    touchScroll: true,
    fontSize: storedFs === null ? undefined : storedFs,
  });
  try {
    frame.src = TermKit.ttyUrl(NAME, storedFs === null ? {} : { fontSize: storedFs });
  } catch (_) {
    note('Not a terminal session name');
  }
  if (typeof ResizeObserver === 'function') new ResizeObserver(() => TermKit.fitSoon(frame)).observe(wrap);
  window.addEventListener('resize', () => TermKit.fitSoon(frame));

  /* ---------- polling: the session and its pane, every 3 s, paused while hidden ---------- */

  let pollTimer = null;
  let nextAt = 0;
  let lastTickAt = 0;
  let polling = false;
  let again = false;
  let baseline = null;
  let failures = 0;

  function pollSoon(ms) {
    if (INTERNAL) return;
    const wait = Math.max(ms, 600 - (Date.now() - lastTickAt));
    const at = Date.now() + wait;
    if (nextAt && nextAt <= at) return;
    nextAt = at;
    clearTimeout(pollTimer);
    pollTimer = setTimeout(tick, wait);
  }

  /* The first shell_version the box reports is the baseline; a different one later means the box was updated while this page stayed open. */
  function checkVersion(v) {
    if (!v) return;
    if (baseline === null) { baseline = v; return; }
    if (v === baseline) return;
    try { sessionStorage.setItem('ccboard:reloaded', '1'); } catch (_) { /* ignore */ }
    location.reload();
  }

  async function tick() {
    nextAt = 0;
    if (document.hidden) return;                                   // visibilitychange restarts the loop
    if (polling) { again = true; return; }
    polling = true;
    lastTickAt = Date.now();
    const [s, p] = await Promise.all([
      api('GET', API).then((v) => ({ v }), (e) => ({ e })),
      api('GET', API + '/pane').then((v) => ({ v }), () => ({ v: null })),   // a missing pane endpoint must never take the session poll down
    ]);
    if (s.v) {
      failures = 0;
      renderHeader(s.v);
      renderStrip(s.v);
      kb.setApproval(TermKit.contextParts(s.v).approve);
      checkVersion(s.v.shell_version);
    } else {
      failures += 1;
      const msg = (s.e && s.e.message) || '';
      if (/not found/i.test(msg)) renderGone('ended');
      else if (failures >= 2) renderGone('offline');
    }
    applyPane(p.v);
    polling = false;
    if (again) { again = false; pollSoon(0); } else pollSoon(POLL_MS);
  }

  document.addEventListener('visibilitychange', () => { if (!document.hidden) { applyLayout(); pollSoon(0); } });
  window.addEventListener('pageshow', (e) => { if (e.persisted) pollSoon(0); });

  /* ---------- go ---------- */

  const vp = TermKit.viewportFit();
  if (window.visualViewport) window.visualViewport.addEventListener('resize', applyLayout);
  window.addEventListener('resize', applyLayout);
  window.addEventListener('orientationchange', () => { baseH = 0; vp.update(); applyLayout(); });
  syncCtx();
  renderQuick();
  applyLayout();
  if (!INTERNAL) pollSoon(0);
})();
