/* ccboard terminal page (v0.5.8): a ttyd iframe made usable on a phone. The header says which session this is (state, model, context) and has the
   text-size buttons; a collapsible strip shows the task and the last prompt; the right-edge rail, the History chip and the key bar scroll and drive
   the pane through POST /api/sessions/<name>/scroll and /keys; a touch swipe on the terminal becomes mouse-wheel events (TermKit.bind); the composer
   (components.js: Enter sends, Shift+Enter or the newline key adds a line) sits above the soft keyboard because body.term is sized by --vvh.
   The session and its pane are polled together every 3 s (paused while the page is hidden). Loaded after core.js, components.js and termkit.js,
   which provide el(), api(), toast(), the composer helpers and TermKit; this file starts everything.
   v0.5.8: a tuning strip under the context strip (one scrolling row: Clear, Compact, Usage, Rename, Context, Status, then Effort and Model ->
   POST /command, only while the session sits at its prompt; the `tune` button in the header collapses it) and a send box that goes through
   POST /prompt (queue while a turn runs). window.TermPage exposes the pure helpers. */
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
  const KEY_TUNE = 'ccboard:term:tune';                       // '1' = the tuning strip is shown, '0' = hidden (default: shown from a 700 px tall window up)
  const TUNE_TALL = 700;
  const TUNE_TALL_WIDE = 560;                                  // the side column (840 px and up) with a mouse: its controls are 28 px, the strip fits a lower window
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

  /* Line 1 is the session's name and nothing else, so it keeps the whole width of the header; line 2 carries the agent glyph, the state badge and the
     project / model / context text (the badge used to share line 1 and cut the name to 't-chec…'). */
  const head = {
    sess: el('span', { class: 'name', text: INTERNAL ? 'login' : (PARTS.length === 3 ? PARTS[2] : NAME) }),
    glyph: el('span'), state: el('span'), meta: el('span', { class: 'l2-text' }), hooks: el('span', { class: 'l2-hooks hidden' }),
    agent: null, stateSig: '', metaSig: '', hm: '',
  };
  head.meta.textContent = INTERNAL ? 'terminal' : (PARTS.length === 3 ? projectLabel(PARTS[0], PARTS[1]) : '');
  $('#headid').append(el('div', { class: 'l1' }, head.sess), el('div', { class: 'l2' }, head.glyph, head.state, head.hooks, head.meta));

  function projectLabel(project, repo) { return repo === 'root' ? project : project + '/' + repo; }

  function setState(node) {
    head.state.textContent = '';
    if (node) head.state.append(node);
  }

  function renderHeader(s) {
    setText(head.sess, s.name || (PARTS.length === 3 ? PARTS[2] : NAME));
    const agent = s.agent || 'shell';
    if (head.agent !== agent) { head.agent = agent; head.glyph.textContent = ''; head.glyph.append(agentGlyph(agent)); }
    if ((s.agent || null) !== quickAg) { quickAg = s.agent || null; renderQuick(); }     // #69: Codex chips for a Codex session, none for a shell
    const badge = stateBadge(s);
    const sig = badge ? badge.textContent + '|' + (s.state || '') : '';
    if (sig !== head.stateSig) { head.stateSig = sig; setState(badge); }
    const hm = s.hooks_missing || '';                              // #96: 'no hooks (untrusted?)', a link to the board's Doctor
    if (hm !== head.hm) {
      head.hm = hm;
      head.hooks.textContent = '';
      if (hm) head.hooks.append(hooksMissingChip(hm));
      head.hooks.classList.toggle('hidden', !hm);
    }
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
    tuneEl.classList.add('hidden');                                // nothing to tune on a session that is gone or unreachable
    tuneApplicable = false;
    syncTune();
  }

  /* ---------- font size ---------- */

  const storedFs = TermKit.clampFont(store.get(KEY_FS));
  const fsVal = el('span', { class: 'vm-val mono', 'aria-live': 'polite', text: String(storedFs || 13) });
  function bumpFont(delta) {
    const t = bound.term();
    const cur = t && t.options && Number(t.options.fontSize) ? Number(t.options.fontSize) : (storedFs || 13);
    const next = TermKit.clampFont(cur + delta);
    if (next === null) return;
    if (bound.setFontSize(next) === null) { note('The terminal is still loading', 'warn'); return; }
    store.set(KEY_FS, String(next));
    setText(fsVal, String(next));
  }

  /* ---------- context strip ---------- */

  const strip = $('#ctxstrip');
  const storedCtx = store.get(KEY_CTX);
  let ctxOn = storedCtx !== '0';
  // the strip costs about 110 px of a phone's 844: seen once, the next visit on a touch screen starts with it folded (the chevron brings it back and that sticks)
  if (storedCtx === null && isCoarse()) store.set(KEY_CTX, '0');
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

  /* The task's phase is an enum (needs_you, in_progress): say it in words, and let the one that is about you wear the amber chip. */
  const PHASE_LABEL = { needs_you: 'needs you', in_progress: 'in progress', pr: 'PR open', backlog: 'backlog', queued: 'queued', merged: 'merged', running: 'running' };
  function phaseChip(phase) {
    const text = ownKey(PHASE_LABEL, phase) ? PHASE_LABEL[phase] : String(phase).replace(/_/g, ' ');
    return el('span', { class: phase === 'needs_you' ? 'state waiting' : 'badge', text });
  }

  function renderStrip(s) {
    const p = TermKit.contextParts(s);
    ctxHas = !!(p.task || p.prompt || p.ask);
    const sig = JSON.stringify(p);
    if (sig !== ctxSig) {
      ctxSig = sig;
      strip.textContent = '';
      if (p.task) strip.append(el('div', { class: 'cx-task' }, p.task, p.phase ? phaseChip(p.phase) : null));
      if (p.prompt) strip.append(el('div', { class: 'cx-prompt', text: '› ' + p.prompt }));
      if (p.ask) strip.append(el('div', { class: 'cx-ask ' + p.askKind, text: (p.askKind === 'message' ? '' : '? ') + p.ask }));
      if (ctxHas && !INTERNAL) {
        strip.append(el('div', { class: 'cx-links' }, link('Open in board', '/#/s/' + ENC), p.taskId !== null ? link('Tasks', '/#/tasks') : null));
      }
    }
    syncCtx();
  }
  // a tap on the strip opens it (the full prompt, and on a phone the links): the strip is a tab stop, Enter or Space on it does the same, and aria-expanded says which
  const stripToggle = () => { strip.classList.toggle('open'); strip.setAttribute('aria-expanded', strip.classList.contains('open') ? 'true' : 'false'); };
  strip.setAttribute('tabindex', '0');
  strip.setAttribute('aria-expanded', 'false');
  strip.addEventListener('click', stripToggle);
  strip.addEventListener('keydown', (e) => {
    if (e.target !== strip || e.isComposing || (e.key !== 'Enter' && e.key !== ' ')) return;          // a link inside keeps its own Enter
    e.preventDefault();
    stripToggle();
  });

  /* ---------- the tuning strip: Compact, Clear, Usage, effort, model, Rename, Context, Status ---------- */

  /* What the strip offers comes from the agent's slash registry (GET /api/agents -> agents.<agent>.slash, once per page load, kept 10 min in
     sessionStorage under AGENTS_KEY as {at, agents}: palette.js reads the same entry). Until it arrives (or when the fetch fails) TermKit's built-in
     registry stands (TermKit.tuneRegistry: the rows of claude.py / codex.py). The command chips follow the registry weight (most used first); weight 0
     stays out unless localStorage ccboard:term:allchips = 1. The settings (effort or reasoning, model, Codex's permissions, Claude's Ultracode switch) come
     from TermKit.tunePlan, the plan the Quad tile's Tune panel draws, and each change is built by TermKit.tuneRequest: POST /tune (the agent's own picker,
     this session only) or POST /command. The strip is only enabled while the session sits at its prompt (TermKit.tuneGate). */
  const AGENTS_KEY = 'ccboard:agents';
  const AGENTS_TTL = 10 * 60 * 1000;
  const KEY_ALLCHIPS = 'ccboard:term:allchips';
  const GATE_TITLE = 'available when the session is at its prompt';
  const PENDING_TTL = 20000;                                   // ms a typed command waits for the statusline that confirms it (the server's 20 s)
  const FLASH_MS = 1500;
  const ARM_MS = 4000;                                         // the second tap on Clear must come within this
  const ESC_GAP_MS = 150;                                      // Escape and the next keys must not arrive together (a TUI reads ESC + key as Alt + key)
  const MODEL_HUE = { opus: 'hue-blue', fable: 'hue-violet', sonnet: 'hue-green', haiku: 'hue-slate' };   // tokens.css .hue-*: the same four as pages/agents.js chipHue('model', ...), which this page does not load
  const PLAN_KEYS = ['effort', 'model', 'reasoning', 'permissions', 'approvals', 'sandbox'];   // registry rows the plan draws as segments, never as command chips

  /* -- pure helpers (window.TermPage, for the tests) -- */

  /* The registry's slash map as an ordered list of {key, cmd, label, arg, read, weight, destructive}: heaviest first, ties in registry order,
     weight 0 left out unless opts.all. */
  function tuneOrder(registry, opts) {
    const reg = registry && typeof registry === 'object' ? registry : {};
    const all = !!(opts && opts.all);
    const out = [];
    Object.keys(reg).forEach((key, i) => {
      const s = reg[key];
      if (!s || typeof s !== 'object') return;
      out.push({ key, cmd: String(s.cmd || '/' + key), label: String(s.label || key), arg: !!s.arg, read: !!s.read, weight: Number(s.weight) || 0, destructive: !!s.destructive, i });
    });
    return out.filter((s) => all || s.weight > 0).sort((a, b) => (b.weight - a.weight) || (a.i - b.i)).map((s) => { delete s.i; return s; });
  }

  /* {show, enabled, title} for a GET /api/sessions/<name> body: TermKit.tuneGate (no strip for a shell or a row without an agent; enabled at the prompt). */
  function tuneGate(row) { return TermKit.tuneGate(row); }

  /* The plan's options with the current one marked: TermKit.tunePlan + TermKit.tuneCurrent (Claude's effort is low..max: ultracode is a switch, not a level). */
  function planOptions(kind, stats) {
    const plan = TermKit.tunePlan('claude', null, stats || {});
    const cur = TermKit.tuneCurrent(stats || {}, plan, null);
    return (plan[kind] ? plan[kind].options : []).map((o) => ({ value: o.value, label: o.label, arg: o.arg, current: !!cur[kind] && cur[kind] === (kind === 'effort' ? o.value.toLowerCase() : o.value) }));
  }
  const effortOptions = (stats) => planOptions('effort', stats);
  const modelOptions = (stats) => planOptions('model', stats);

  /* How the send box talks to the session: 'queue' while a turn runs (/prompt with queue:true: Claude queues the text), 'prompt' at the prompt,
     'keys' (the raw /keys path of old) for a shell, a row that has not reported yet and every dialog (a permission, a question), where typed text
     answers the dialog instead of going to a composer. */
  function sendMode(row) {
    if (!row || !row.agent || row.agent === 'shell') return 'keys';
    const flags = row.flags && typeof row.flags === 'object' ? row.flags : {};
    if (Array.isArray(row.pending) && row.pending.length) return 'keys';
    if (row.state === 'working') return 'queue';
    if (row.state === 'waiting') return flags.wait_kind === 'idle' ? 'prompt' : 'keys';
    return row.state === 'idle' || row.state === 'done' || row.state === 'errored' ? 'prompt' : 'keys';
  }
  const queueLabel = (row) => (sendMode(row) === 'queue' ? 'queue' : 'Send');

  window.TermPage = { tuneOrder, tuneGate, effortOptions, modelOptions, queueLabel, sendMode, GATE_TITLE, AGENTS_KEY, MODEL_HUE };

  /* -- the registry -- */

  const sstore = {
    get(k) { try { return sessionStorage.getItem(k); } catch (_) { return null; } },
    set(k, v) { try { sessionStorage.setItem(k, v); } catch (_) { /* storage may be unavailable */ } },
  };
  function cachedAgents() {
    try {
      const v = JSON.parse(sstore.get(AGENTS_KEY) || 'null');
      if (v && typeof v.at === 'number' && Date.now() - v.at < AGENTS_TTL && v.agents && typeof v.agents === 'object') return v.agents;
    } catch (_) { /* a bad cache entry reads as no cache */ }
    return null;
  }
  let agentsMap = cachedAgents();
  let agentsAsked = false;
  let lastRow = null;                                          // the latest GET /api/sessions/<name> body (the send box and the gate read it)

  function loadAgents() {
    if (agentsAsked || agentsMap) return;                      // once per page load; a fresh cache needs no fetch at all
    agentsAsked = true;
    api('GET', '/api/agents').then((r) => {
      const m = r && r.agents && typeof r.agents === 'object' ? r.agents : null;
      if (!m) return;
      agentsMap = m;
      sstore.set(AGENTS_KEY, JSON.stringify({ at: Date.now(), agents: m }));
      if (lastRow) renderTune(lastRow);
    }, () => { /* the embedded list stands */ });
  }

  function schemaFor(agent) {
    const a = agentsMap && agentsMap[agent];
    return a && typeof a === 'object' ? a : null;
  }
  function slashFor(agent) {
    const reg = TermKit.tuneRegistry(agent, schemaFor(agent));
    return reg && Object.keys(reg).length ? reg : null;
  }

  /* -- commands -- */

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  let claudeDialog = false;                                    // a read command left Claude's own dialog open: Escape closes it
  let escPending = null;
  let tuneBusy = false;

  /* A read command (/usage, /context, /status, /cost) leaves Claude's dialog on the pane. Escape it once (twice would open the rewind menu) when
     the readout is dismissed, and before any /command or /prompt that has not seen that happen. */
  function settleEscape() {
    if (claudeDialog) { claudeDialog = false; escPending = sendKeys(['Escape']).then(() => sleep(ESC_GAP_MS)); }
    const p = escPending;
    if (!p) return Promise.resolve();
    return p.then(() => { if (escPending === p) escPending = null; });
  }

  /* 409 {error, message, state, wait_kind, retry}: the board will not type into the pane right now. */
  function refused(e, what) {
    const b = e && e.body && typeof e.body === 'object' ? e.body : null;
    if (e && e.status === 409 && b) {
      note((b.message || b.error || 'refused') + (typeof b.retry === 'number' ? ' · try again in ' + b.retry + ' s' : ''), 'warn');
    } else if (e && e.status === 404 && !(b && b.error)) {
      note('This board is too old for ' + what + ': update ccboard');
    } else note((e && e.message) || 'Failed');
    pollSoon(0);                                               // the gate was probably stale: read the row again
  }

  let pendLocal = null;                                        // {cmd, arg, base, sat}: the set command this page typed and still waits on
  let pendTimer = null;
  let reacted = '';                                            // the last_cmd.at already answered with a flash or a toast

  function itemsFor(cmd, arg) { return tuneItems.filter((it) => it.cmd === cmd && (it.arg === undefined || it.arg === (arg || ''))); }

  function startPending(spec, arg) {
    const lc = lastRow && lastRow.flags && lastRow.flags.last_cmd;
    pendLocal = { cmd: spec.key, arg: arg || '', base: lc && lc.at ? String(lc.at) : '', sat: '' };
    clearTimeout(pendTimer);
    pendTimer = setTimeout(() => finishPending(false), PENDING_TTL);
    for (const it of itemsFor(spec.key, arg)) it.node.classList.add('pending');
  }

  function finishPending(ok) {
    const p = pendLocal;
    if (!p) return;
    pendLocal = null;
    clearTimeout(pendTimer);
    pendTimer = null;
    const nodes = itemsFor(p.cmd, p.arg).map((it) => it.node);
    for (const n of nodes) n.classList.remove('pending');
    if (ok) {
      for (const n of nodes) n.classList.add('ok');
      setTimeout(() => { for (const n of nodes) n.classList.remove('ok'); }, FLASH_MS);
    } else note('/' + p.cmd + (p.arg ? ' ' + p.arg : '') + ' not confirmed by the statusline', 'warn');
  }

  /* On each poll: the server's flags.pending_cmd / flags.last_cmd settle the command this page typed. */
  function settlePending(flags) {
    if (!pendLocal) return;
    const pc = flags.pending_cmd && typeof flags.pending_cmd === 'object' ? flags.pending_cmd : null;
    if (pc && pc.cmd === pendLocal.cmd) pendLocal.sat = String(pc.at || '');            // the server's stamp of OUR command: last_cmd.at will carry it
    const lc = flags.last_cmd && typeof flags.last_cmd === 'object' ? flags.last_cmd : null;
    if (!lc || lc.cmd !== pendLocal.cmd) return;
    const at = String(lc.at || '');
    const ours = pendLocal.sat ? at === pendLocal.sat : (at !== pendLocal.base && String(lc.arg || '') === pendLocal.arg);
    if (!ours || at === reacted) return;
    reacted = at;
    finishPending(lc.confirmed === true);
  }

  async function runTune(spec, o) {
    const opts = o || {};
    if (tuneBusy) return;
    tuneBusy = true;
    disarm();
    try {
      await settleEscape();
      if (opts.group && opts.group.via === 'restart') {                                       // Codex's reasoning, approvals, sandbox: preview the command, confirm, restart (issue #2)
        await TermKit.tuneRestart({ tmux: NAME, agent: lastRow && lastRow.agent, touch: isCoarse(), onRestart: reloadFrame }, opts.group, opts.arg, opts.anchor || null);
        pollSoon(500);
        return;
      }
      let req;
      if (opts.group) req = TermKit.tuneRequest(opts.group, opts.arg);                       // a setting: the shared request (POST /tune or /command)
      else {
        const body = { cmd: spec.key };
        if (opts.arg) body.arg = opts.arg;
        if (spec.destructive && opts.confirm) body.confirm = true;
        req = { path: '/command', body };
      }
      let res = null;
      try { res = await api('POST', API + req.path, req.body); } catch (e) { refused(e, 'tuning'); return; }
      if (res && typeof res.screen === 'string') { claudeDialog = res.dialog !== false; openReadout(spec, res.screen); }   // printed inline (Codex /status, Claude /context): nothing to Escape
      else if (req.path === '/tune' && res && res.confirmed === true) { startPending(spec, opts.arg); finishPending(true); }
      else if (req.path === '/tune' && res && res.confirmed === false) note(res.message || (spec.key + ' not confirmed by the session'), 'warn');
      else if (!spec.read) startPending(spec, opts.arg);
      pollSoon(500);
    } finally { tuneBusy = false; }
  }

  /* a restarted session is a new tmux session of the same name: the terminal frame attaches to it afresh (after a moment, so the new session exists) */
  function reloadFrame() {
    setTimeout(() => { try { frame.src = TermKit.ttyUrl(NAME, storedFs === null ? {} : { fontSize: storedFs }); } catch (_) { /* not a terminal name: nothing to attach to */ } }, 1500);
  }

  /* -- the readout: a bottom sheet with what a read command printed -- */

  let readout = null;
  function closeDialog(d) { try { if (d.open) d.close(); else d.removeAttribute('open'); } catch (_) { d.removeAttribute('open'); } }
  function showDialog(d) { try { d.showModal(); } catch (_) { d.setAttribute('open', ''); } }

  function openReadout(spec, text) {
    if (!readout) {
      const title = el('h2', { class: 'ro-title' });
      const pre = el('pre', { class: 'ro-text' });
      const dlg = el('dialog', { class: 'readout', 'aria-label': 'Command output' },
        el('div', { class: 'ro-head' }, title), pre,
        el('div', { class: 'ro-actions' }, el('button', { type: 'button', class: 'ro-close', text: 'Close', onclick: () => closeDialog(dlg) })));
      dlg.addEventListener('click', (e) => { if (e.target === dlg) closeDialog(dlg); });          // no padding on the dialog: a click on itself is the backdrop
      dlg.addEventListener('close', () => { settleEscape(); });                                   // Close, Esc and the backdrop all end here
      document.body.append(dlg);
      readout = { dlg, title, pre };
    }
    setText(readout.title, spec.cmd);
    readout.pre.textContent = String(text).replace(/\s+$/, '') || '(nothing printed)';
    readout.pre.scrollTop = 0;
    showDialog(readout.dlg);
  }

  /* -- rename: one input in a small dialog at the top (the soft keyboard must not cover it) -- */

  let renameDlg = null;
  function openRename(spec) {
    if (!renameDlg) {
      const input = el('input', { id: 'rn-input', type: 'text', maxlength: '120', autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false', enterkeyhint: 'done', 'aria-describedby': 'rn-err', placeholder: 'session name' });
      const err = el('p', { id: 'rn-err', class: 'rn-err bad', role: 'alert' });              // the error belongs under the field it is about, never only in a toast
      const dlg = el('dialog', { class: 'rename-dlg', 'aria-label': 'Rename the session' });
      const form = el('form', { class: 'rn-form', onsubmit: (e) => {
        e.preventDefault();
        const v = input.value.trim();
        if (!v) { err.textContent = 'Type a name first.'; input.setAttribute('aria-invalid', 'true'); try { input.focus(); } catch (_) { /* no focus */ } return; }
        closeDialog(dlg);
        runTune(renameDlg.spec, { arg: v });
      } },
        el('label', { class: 'rn-label', for: 'rn-input', text: 'Rename the session' }), input, err,
        el('div', { class: 'ro-actions' }, el('button', { type: 'button', text: 'Cancel', onclick: () => closeDialog(dlg) }), el('button', { type: 'submit', class: 'primary', text: 'Rename' })));
      input.addEventListener('input', () => { err.textContent = ''; input.removeAttribute('aria-invalid'); });
      dlg.append(form);
      dlg.addEventListener('click', (e) => { if (e.target === dlg) closeDialog(dlg); });
      document.body.append(dlg);
      renameDlg = { dlg, input, err, spec };
    }
    renameDlg.spec = spec;
    renameDlg.err.textContent = '';
    renameDlg.input.removeAttribute('aria-invalid');
    renameDlg.input.value = (lastRow && lastRow.stats && lastRow.stats.session_name) || '';
    showDialog(renameDlg.dlg);
    try { renameDlg.input.focus(); if (typeof renameDlg.input.select === 'function') renameDlg.input.select(); } catch (_) { /* no focus */ }
  }

  /* -- the strip itself: ONE horizontally scrolling row (about 52 px on a phone, so the terminal keeps its height): the command chips in weight
     order, then the Effort segment, then the Model segment. Built once per registry, then patched in place by every poll (a rebuild would
     swallow a tap in flight). A fade at the edge that has more behind it (classes more-l / more-r on #tune, from scrollLeft / scrollWidth like a
     scroll shadow) says "swipe", and the current effort and model chips are scrolled into view once after the first render (and again when the
     strip is reopened after a collapse). A `tune` button in the
     header collapses the strip (localStorage ccboard:term:tune, '1' shown / '0' hidden; default shown from a 700 px tall window up). From 840 px the
     strip is two segmented controls over a command grid in the side column instead of a scrolling row (term.css). -- */

  const tuneEl = keep(el('div', { id: 'tune', class: 'hidden', role: 'toolbar', 'aria-label': 'Tune this session' }));   // the gaps and the dimmed chips must not blur the composer either
  if (!INTERNAL) $('#termmain').insertBefore(tuneEl, $('#ttywrap'));
  let tuneSig = '';
  let tuneGated = false;                                       // the strip is dimmed: a tap on it (pointer-events:none on the chips lets it through) says why
  tuneEl.addEventListener('click', () => { if (tuneGated) note(GATE_TITLE.charAt(0).toUpperCase() + GATE_TITLE.slice(1), 'info'); });
  let tuneItems = [];                                          // [{node, kind, cmd, arg?, value?}]
  let tuneRow = null;                                          // the scrolling .tune-row of the current build
  let tuneApplicable = false;                                  // the gate shows a strip for this row (a shell, a gone session and the login terminal have none)
  let tuneSeen = false;                                        // the current effort and model chips have been scrolled into view
  let stalePending = '';                                       // the server's pending_cmd.at already toasted as "not confirmed" (see renderTune)
  let armed = false;                                           // Clear waits for its second tap
  let armTimer = null;

  /* The height under which the strip starts hidden: 700 px on a phone and on any touch screen (44 px controls); 560 px in the side column with a
     mouse or trackpad, where the strip is three compact rows and the column gives up its reply chips and context first (term.css). */
  function tuneTall() {
    const mq = (q) => { try { return typeof window.matchMedia === 'function' && window.matchMedia(q).matches; } catch (_) { return false; } };
    if (!mq('(min-width:840px)')) return TUNE_TALL;
    const coarse = document.documentElement.classList.contains('force-coarse') || mq('(pointer:coarse)');
    return coarse ? TUNE_TALL : TUNE_TALL_WIDE;
  }

  /* shown: the person's choice ('1' / '0'); without one, a window under 700 px tall starts with it hidden (a phone with the browser's own bars) */
  function tuneWanted() {
    const v = store.get(KEY_TUNE);
    if (v === '1') return true;
    if (v === '0') return false;
    const h = Number(window.innerHeight);
    return !(h > 0 && h < tuneTall());                         // an unknown height counts as tall
  }

  const tuneBtn = keep(el('button', { class: 'minimal tune-toggle hidden', type: 'button', text: 'tune', title: 'Show or hide the tuning strip', 'aria-label': 'Show or hide the tuning strip', 'aria-pressed': 'true', onclick: () => {
    store.set(KEY_TUNE, tuneWanted() ? '0' : '1');
    syncTune();
  } }));

  /* the scroll shadow: an edge fades when the row has more behind it */
  function tuneFade() {
    const r = tuneRow;
    if (!r) return;
    const room = (Number(r.scrollWidth) || 0) - (Number(r.clientWidth) || 0);
    const left = Number(r.scrollLeft) || 0;
    tuneEl.classList.toggle('more-r', room > 1 && left < room - 1);
    tuneEl.classList.toggle('more-l', room > 1 && left > 1);
  }

  /* once: the current effort chip, the Model label and the current model chip, nearest edge only (no centring, no smooth scroll): on a 390 px
     phone Effort and Model sit past the command chips, so without this the person would not see what is set (and `model` would be unlabelled). */
  function revealCurrent() {
    if (tuneSeen || !tuneRow) return;
    const cur = (kind) => tuneItems.find((it) => it.kind === kind && it.node.classList.contains('on'));
    const eff = cur('effort');
    const mod = cur('model');
    if (!eff && !mod) return;                                  // the statusline has not said yet: a later poll tries again
    if (typeof tuneEl.getClientRects === 'function' && tuneEl.getClientRects().length === 0) return;   // not laid out (collapsed, or the soft keyboard is up)
    tuneSeen = true;
    const show = (n) => { if (n && typeof n.scrollIntoView === 'function') { try { n.scrollIntoView({ inline: 'nearest', block: 'nearest' }); } catch (_) { /* an engine without the options form */ } } };
    if (eff) show(eff.node);
    if (mod) { show(mod.node.parentNode && mod.node.parentNode.querySelector('.tune-lbl')); show(mod.node); }
    tuneFade();
  }

  /* collapsed / shown, the toggle's state, then the fade and the reveal for a strip that is on screen */
  function syncTune() {
    const on = tuneWanted();
    tuneEl.classList.toggle('collapsed', !on);                 // not `hidden`: renderTune owns that one (gone, shell, no registry)
    tuneBtn.classList.toggle('hidden', !tuneApplicable);
    tuneBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
    if (!on) tuneSeen = false;                                 // a display:none row forgets its scroll position: bring the current chips into view again when it is reopened
    if (tuneApplicable && on) { tuneFade(); revealCurrent(); }
  }

  function disarm() {
    if (!armed) return;
    armed = false;
    clearTimeout(armTimer);
    armTimer = null;
    for (const it of tuneItems) if (it.kind === 'clear') { it.node.classList.remove('confirm'); setText(it.node, it.label); }
    tuneFade();
  }

  const tuneChip = (spec, label, onTap) => keep(el('button', { type: 'button', class: 'tune-chip', 'data-cmd': spec.key, text: label, onclick: onTap }));

  function buildTune(specs, plan) {
    tuneEl.textContent = '';
    tuneItems = [];
    tuneSeen = false;
    armed = false;
    const row = el('div', { class: 'tune-row' });
    const segs = {};                                           // effort and model go after the command chips, always in that order (then Codex's permissions)
    /* one segment per plan group: kind effort | model | perms; every tap is TermKit.tuneRequest's request */
    const seg = (kind, title, group) => {
      const spec = { key: group.cmd, cmd: '/' + group.cmd, label: title, read: false };
      const g = el('div', { class: 'tune-seg', role: 'group', 'aria-label': title }, el('span', { class: 'tune-lbl', text: title.toLowerCase() }));
      for (const op of group.options) {
        const node = tuneChip(spec, op.label, () => runTune(spec, { arg: op.arg, group, anchor: node }));
        node.classList.add('tune-opt');
        node.setAttribute('data-via', group.via);
        if (kind === 'model' && ownKey(MODEL_HUE, op.value)) node.classList.add('tune-model', MODEL_HUE[op.value]);
        const title2 = (group.via === 'tune' ? title + ' ' + op.label + ' for this session only' : group.via === 'restart' ? 'Restart with ' + title.toLowerCase() + ' ' + op.label : '/' + group.cmd + ' ' + op.arg) + (group.note ? ' (' + group.note.toLowerCase() + ')' : '')
          + (group.unverified ? ' · unverified key path: the result is read back from the screen' : '');
        tuneItems.push({ node, kind, cmd: group.cmd, arg: op.arg, value: op.value, label: op.label, title: title2 });
        g.append(node);
      }
      // said in words under the group in the wide column (term.css shows .tune-note there only): a hover title alone never reaches a touch screen
      if (group.note) g.append(el('p', { class: 'tune-note', text: group.note }));
      if (group.unverified) g.append(el('p', { class: 'tune-note', text: 'Unverified key path: the board reads the result back from the screen' }));
      return g;
    };
    const codex = lastRow && lastRow.agent === 'codex';
    if (plan.effort) segs.effort = seg('effort', codex ? 'Reasoning' : 'Effort', plan.effort);
    if (plan.model) segs.model = seg('model', 'Model', plan.model);
    if (plan.approvals) segs.approvals = seg('approvals', 'Approvals', plan.approvals);
    if (plan.sandbox) segs.sandbox = seg('sandbox', 'Sandbox', plan.sandbox);
    for (const s of specs) {
      if (PLAN_KEYS.includes(s.key)) continue;                 // drawn as segments from the plan
      if (s.key === 'rename') {
        const node = tuneChip(s, s.label, () => { disarm(); openRename(s); });
        tuneItems.push({ node, kind: 'rename', cmd: s.key, label: s.label });
        row.append(node);
      } else if (s.destructive) {
        const node = tuneChip(s, s.label, () => {
          if (!armed) {                                        // first tap: ask; the second one, within ARM_MS, sends it with confirm
            armed = true;
            node.classList.add('confirm');
            setText(node, 'Confirm ' + s.label.toLowerCase());
            armTimer = setTimeout(disarm, ARM_MS);
            tuneFade();
            return;
          }
          runTune(s, { confirm: true });
        });
        node.classList.add('destructive');                     // red-outlined at rest; the armed 'Confirm clear' is the solid fill (term.css)
        tuneItems.push({ node, kind: 'clear', cmd: s.key, label: s.label });
        row.append(node);
      } else if (s.key === 'fast') {                           // `/fast on` or `/fast off`: bare /fast opens a dialog that swallows keys
        const node = tuneChip(s, s.label, () => {
          disarm();
          const on = !!(lastRow && lastRow.stats && lastRow.stats.fast === true);
          runTune(s, { arg: on ? 'off' : 'on' });
        });
        tuneItems.push({ node, kind: 'fast', cmd: s.key, label: s.label, title: '/fast on or /fast off' });
        row.append(node);
      } else {
        const node = tuneChip(s, s.label, () => { disarm(); runTune(s, {}); });
        tuneItems.push({ node, kind: 'cmd', cmd: s.key, label: s.label, title: s.cmd + (s.read ? ' (shows what it prints)' : '') });
        row.append(node);
      }
    }
    if (plan.ultra && plan.effort) {                           // Claude's Ultracode switch: a setting, not an effort level (/effort ultracode on | off)
      const group = { ...plan.effort, setting: 'ultracode' };
      const spec = { key: plan.effort.via === 'tune' ? 'ultracode' : 'effort', cmd: '/effort', label: 'Ultracode', read: false };
      const node = tuneChip(spec, 'Ultracode', () => {
        disarm();
        const on = TermKit.tuneCurrent(lastRow && lastRow.stats, plan, lastRow && lastRow.flags).ultra === 'on';
        const want = on ? 'off' : 'on';
        runTune(spec, group.via === 'tune' ? { arg: want, group } : { arg: 'ultracode ' + want, group: { ...group, via: 'command' } });
      });
      tuneItems.push({ node, kind: 'ultra', cmd: spec.key, arg: group.via === 'tune' ? undefined : null, label: 'Ultracode', title: 'Ultracode sets xhigh and turns workflows on, for this session only' });   // an older server's inline form never marks the effort chips pending
      row.append(node);
    }
    if (typeof setAutoContinue === 'function' && lastRow && lastRow.agent !== 'shell') {   // #84: a board switch (POST /flags), not a typed command: the prompt gate does not hold it
      const node = tuneChip({ key: 'autocontinue' }, 'Auto-continue', () => {
        disarm();
        const off = !(lastRow && lastRow.flags && lastRow.flags.no_autoresume);
        setAutoContinue(NAME, off, { apply: (v) => {          // optimistic on this page's copy of the row; the next poll brings the board's
          const flags = Object.assign({}, (lastRow && lastRow.flags) || {});
          if (v) flags.no_autoresume = true; else delete flags.no_autoresume;
          renderTune(Object.assign({}, lastRow, { flags }));
        } });
      });
      tuneItems.push({ node, kind: 'auto', cmd: 'autocontinue', label: 'Auto-continue', title: AUTO_CONTINUE_WHAT });
      row.append(node);
    }
    if (codex) {                                               // Codex: Model, Reasoning, Approvals, Sandbox, the order of the quad tile's Tune (Claude keeps Effort then Model)
      if (segs.model) row.append(segs.model);
      if (segs.effort) row.append(segs.effort);
    } else {
      if (segs.effort) row.append(segs.effort);
      if (segs.model) row.append(segs.model);
    }
    if (plan.approvals || plan.sandbox) row.classList.add('tune-wrap');     // four sections do not fit one phone-wide scroller: they wrap (term.css, under 840 px)
    if (segs.approvals) row.append(segs.approvals);
    if (segs.sandbox) row.append(segs.sandbox);
    row.addEventListener('scroll', tuneFade);
    tuneRow = row;
    tuneEl.append(row);
  }

  /* A server flags.pending_cmd is only believed for PENDING_TTL after its own `at` stamp: the server clears it when a statusline arrives, and an
     idle Claude may send none, so a reload long after the command would otherwise show the chip pending for ever. An `at` that does not parse
     reads as fresh. */
  function pendingFresh(pc) {
    const age = Date.now() - Date.parse(pc.at);
    return !(age >= PENDING_TTL);                              // NaN (no stamp, or one that does not parse) is fresh
  }

  function renderTune(s) {
    lastRow = s;
    const gate = tuneGate(s);
    const slash = gate.show ? slashFor(s.agent) : null;
    const specs = slash ? tuneOrder(slash, { all: store.get(KEY_ALLCHIPS) === '1' }) : [];
    const plan = gate.show ? TermKit.tunePlan(s.agent, schemaFor(s.agent) || (slash ? { slash } : null), s.stats || {}) : null;
    const groups = plan ? [plan.effort, plan.model, plan.approvals, plan.sandbox].filter(Boolean) : [];
    if (!gate.show || (!specs.length && !groups.length) || INTERNAL) { tuneEl.classList.add('hidden'); tuneApplicable = false; syncTune(); return; }
    loadAgents();
    const sig = s.agent + JSON.stringify([specs, groups.map((g) => [g.cmd, g.via, g.options.map((o) => o.value)]), plan.ultra]);
    if (sig !== tuneSig) { tuneSig = sig; buildTune(specs, plan); }
    tuneEl.classList.remove('hidden');
    tuneApplicable = true;
    const flags = s.flags && typeof s.flags === 'object' ? s.flags : {};
    const stats = s.stats && typeof s.stats === 'object' ? s.stats : {};
    const pc = flags.pending_cmd && typeof flags.pending_cmd === 'object' ? flags.pending_cmd : null;
    const live = !!pc && pendingFresh(pc);
    const now = TermKit.tuneCurrent(stats, plan, flags);
    tuneGated = !gate.enabled;
    if (tuneGated) disarm();
    for (const it of tuneItems) {
      const n = it.node;
      if (it.kind === 'auto') {                                // #84: always usable; its words say the state, aria-pressed and the tint repeat it
        const off = !!flags.no_autoresume;
        n.disabled = false;
        setText(n, 'Auto-continue: ' + (off ? 'off' : 'on'));
        n.classList.toggle('on', !off);
        n.setAttribute('aria-pressed', off ? 'false' : 'true');
        n.setAttribute('title', AUTO_CONTINUE_WHAT + (boardAutoContinueOff() ? ' ' + AUTO_CONTINUE_BOARD_OFF : ''));
        continue;
      }
      if (n.disabled === gate.enabled) n.disabled = !gate.enabled;
      const title = gate.enabled ? (it.title || '') : gate.title;
      if (title) n.setAttribute('title', title); else n.removeAttribute('title');
      let on = false;
      const seg = it.kind === 'effort' || it.kind === 'model' || it.kind === 'approvals' || it.kind === 'sandbox';
      const have = it.kind === 'approvals' ? now.approval : now[it.kind];
      if (seg) on = !!have && have === (it.kind === 'effort' ? String(it.value).toLowerCase() : it.value);
      else if (it.kind === 'fast') on = now.fast === true;
      else if (it.kind === 'ultra') on = now.ultra === 'on';
      n.classList.toggle('on', on);
      if (seg || it.kind === 'fast' || it.kind === 'ultra') n.setAttribute('aria-pressed', on ? 'true' : 'false');
      const mine = !!(pendLocal && pendLocal.cmd === it.cmd && (it.arg === undefined || it.arg === pendLocal.arg));
      const theirs = live && pc.cmd === it.cmd && (it.arg === undefined || it.arg === String(pc.arg || ''));
      n.classList.toggle('pending', mine || theirs);
    }
    if (pc && !live && stalePending !== String(pc.at) && !(pendLocal && pendLocal.cmd === pc.cmd)) {   // aged out: say so once (a command this page typed toasts through its own timer)
      stalePending = String(pc.at);
      note('/' + pc.cmd + (pc.arg ? ' ' + pc.arg : '') + ' not confirmed by the statusline', 'warn');
    }
    settlePending(flags);
    syncTune();
  }

  /* -- sending: /prompt (queue-aware, multi-line safe) with the old /keys path for dialogs and older servers -- */

  async function sendPrompt(text) {
    if (sendMode(lastRow) === 'keys') return sendText(text, true);
    await settleEscape();
    return promptOnce(text, sendMode(lastRow) === 'queue', false);
  }

  async function promptOnce(text, queue, retried) {
    try {
      const r = await api('POST', API + '/prompt', { text, enter: true, queue });
      if (r && r.queued) note('queued', 'ok');
      pollSoon(500);
      return true;
    } catch (e) {
      if (e && e.status === 404) return sendText(text, true);                                       // an older server has no /prompt
      if (e && e.status === 409 && e.body && e.body.error === 'working' && !queue && !retried) return promptOnce(text, true, true);   // a turn started since the last poll: queue it
      refused(e, 'sending');
      return false;
    }
  }

  /* ---------- scroll rail and the History chip ---------- */

  const wrap = $('#ttywrap');
  const chip = $('#histchip');
  chip.textContent = '';
  chip.append('History', el('span', { class: 'dim', text: 'tap for live' }));
  TermKit.pressable(chip, () => scroll('exit'));
  chip.removeAttribute('tabindex');

  function railButton(icon, label, dir, repeat) {
    const b = el('button', { type: 'button', class: 'rail-btn' + (dir === 'bottom' ? ' rail-bottom' : ''), 'aria-label': label, title: label }, ic(icon));
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
    wrap.classList.toggle('scrolled', inMode);                      // history mode: the rail is fully visible and its 'bottom' button leads
    chip.classList.toggle('hidden', !inMode);
    if (inMode) chip.setAttribute('aria-hidden', 'false'); else chip.setAttribute('aria-hidden', 'true');
  }

  /* ---------- key bar, key mode and the soft keyboard ---------- */

  const kb = TermKit.keyBar($('#keyhost'), { send: sendKeys, sendText, compact: false });    // the scroll keys live on the rail
  let lastCompact = false;
  let baseH = 0;
  let baseW = 0;

  function keysOverride() { const v = store.get(KEY_KEYS); return v === 'compact' || v === 'full' ? v : null; }

  function isCoarse() {
    try { return document.documentElement.classList.contains('force-coarse') || window.matchMedia('(pointer:coarse)').matches; } catch (_) { return false; }
  }

  /* the key-bar mode as a segmented control (one track, the current option tinted), not a button that cycles through three words */
  const KEY_MODES = [[null, 'auto', 'Compact while the soft keyboard is up or the window is short, both rows otherwise'],
    ['compact', 'compact', 'Always the one-row key bar'], ['full', 'full', 'Always both rows']];
  const keyModeBtns = KEY_MODES.map(([v, label, title]) => keep(el('button', { type: 'button', class: 'small vm-opt', 'data-mode': v || 'auto', title, 'aria-pressed': 'false', text: label,
    onclick: () => { store.set(KEY_KEYS, v); applyLayout(); } })));
  const keyModeSeg = el('div', { class: 'vm-ctl vm-seg', role: 'group', 'aria-label': 'Key bar' }, ...keyModeBtns);

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
    for (const b of keyModeBtns) { const on = b.getAttribute('data-mode') === (mode || 'auto'); b.classList.toggle('on', on); b.setAttribute('aria-pressed', on ? 'true' : 'false'); }
  }

  /* ---------- quick replies ---------- */

  /* The list lives in components.js (quickLoad / quickSave, key ccboard:quick:<session>, shared with the peek) and is edited in a <dialog>
     (quickReplyEditor): the pencil button or a long press on a chip. A tap on a chip sends it with Enter. */

  /* While a permission waits the amber 'y ⏎ / n ⏎' row already answers it: the quick row does not offer y / yes / n a second time. */
  let approvalOn = false;
  const APPROVE_WORDS = ['y', 'yes', 'n', 'no'];
  let quickAg = null;                                               // the session's agent (renderHeader): the chips are that agent's defaults (#69)

  /* A chip cut off at the right edge fades out, so it reads as 'more this way'; the fade goes when the row fits or is scrolled to its end. */
  function syncQuickFade() {
    const q = $('#quick');
    q.classList.toggle('fade', q.scrollWidth > q.clientWidth + 1 && q.scrollLeft + q.clientWidth < q.scrollWidth - 1);
  }

  function renderQuick() {
    const q = $('#quick');
    q.textContent = '';
    for (const t of quickLoad(NAME, quickAg)) {
      if (approvalOn && APPROVE_WORDS.includes(String(t).trim().toLowerCase())) continue;
      q.append(keep(quickChip(t, { onSend: () => sendText(t, true), onEdit: editQuick })));
    }
    syncQuickFade();
  }
  $('#quick').addEventListener('scroll', syncQuickFade);
  window.addEventListener('resize', syncQuickFade);

  function editQuick() {
    quickReplyEditor({ items: quickLoad(NAME, quickAg), defaults: quickDefaults(quickAg), onSave: (items) => { quickSave(NAME, items, quickAg); renderQuick(); } });
  }

  function setApproval(on) {
    kb.setApproval(on);
    if (approvalOn !== !!on) { approvalOn = !!on; renderQuick(); }
  }

  $('#qtools').append(keep(el('button', { type: 'button', class: 'icon minimal', title: 'Edit quick replies', 'aria-label': 'Edit quick replies', onclick: editQuick }, ic('edit'))));

  /* ---------- composer ---------- */

  /* Enter sends, Shift+Enter or the newline key adds a line (components.js composer). Text goes through POST /prompt (one bracketed paste, so
     Claude Code keeps the newlines inside the prompt; queue:true while a turn runs, and the button then reads "queue"); sendPrompt falls back
     to raw keys for a shell, a dialog and an older server. An empty box plus Enter forwards a bare Enter. */
  const sendBox = $('#sendtext');
  function submitSend() {
    const v = sendBox.value.replace(/\r\n?/g, '\n');
    if (v.trim()) {
      sendBox.value = '';
      composerGrow(sendBox);
      sendPrompt(v).then((ok) => { if (!ok && !sendBox.value) { sendBox.value = v; composerGrow(sendBox); } });   // a refused send keeps the text
    } else { sendKeys(['Enter']); }
  }
  composerBind(sendBox, { onSend: submitSend });
  $('#sendbtn').setAttribute('title', 'Send (an empty box sends Enter)');
  $('#sendform').addEventListener('submit', (e) => { e.preventDefault(); submitSend(); });
  keep($('#nl')).addEventListener('click', () => composerInsertNewline(sendBox));
  keep($('#sendbtn'));
  sendBox.addEventListener('focus', () => wrap.classList.remove('active'));

  /* ---------- header buttons, back ---------- */

  /* One 'Aa' button instead of A− and A+ (they were two of the four 44 px controls that squeezed the session name): it opens a small panel under the
     header with the text size and the key-bar mode. Tapping outside or Esc closes it. term.html has no shared menu, so this is the page's own. */
  const viewMenu = el('div', { id: 'viewmenu', class: 'viewmenu hidden', role: 'group', 'aria-label': 'View options' },
    el('div', { class: 'vm-row' }, el('span', { class: 'vm-k', text: 'Text size' }),
      el('div', { class: 'vm-ctl vm-step' },
        keep(el('button', { class: 'small fs', type: 'button', 'aria-label': 'Smaller text', title: 'Smaller text', onclick: () => bumpFont(-1) }, 'A−')),
        fsVal,
        keep(el('button', { class: 'small fs', type: 'button', 'aria-label': 'Larger text', title: 'Larger text', onclick: () => bumpFont(1) }, 'A+')))),
    el('div', { class: 'vm-row' }, el('span', { class: 'vm-k', text: 'Key bar' }), keyModeSeg));
  const viewBtn = keep(el('button', { class: 'minimal fs', type: 'button', 'aria-label': 'Text size and key bar', title: 'Text size and key bar', 'aria-expanded': 'false', 'aria-haspopup': 'true',
    onclick: () => toggleViewMenu() }, 'Aa'));
  function toggleViewMenu(force) {
    const on = typeof force === 'boolean' ? force : viewMenu.classList.contains('hidden');
    viewMenu.classList.toggle('hidden', !on);
    viewBtn.setAttribute('aria-expanded', on ? 'true' : 'false');
  }
  document.addEventListener('pointerdown', (e) => {
    if (viewMenu.classList.contains('hidden') || viewMenu.contains(e.target) || viewBtn.contains(e.target)) return;
    toggleViewMenu(false);
  }, true);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !viewMenu.classList.contains('hidden')) toggleViewMenu(false); });
  $('#termhead').append(viewMenu);
  $('#headtools').append(viewBtn, tuneBtn, ctxBtn);

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
  window.addEventListener('resize', tuneFade);                     // a turn of the phone changes how much of the strip fits

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
      renderTune(s.v);
      setText($('#sendbtn'), queueLabel(s.v));
      setApproval(TermKit.contextParts(s.v).approve);
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
