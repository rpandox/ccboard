/* ccboard termkit (v0.5.8, terminal page v2): everything the terminal page needs around a ttyd iframe, as one namespace.
   Definition only, like core.js and components.js: loading this file touches no DOM, storage, network, listener or timer; the
   page script (term.js) calls into it. Classic script (no modules). Loaded by term.html between components.js and term.js.

     TermKit.ttyUrl(name, {mode, fontSize, renderer, quiet})   the iframe URL for ttyd (mode whitelisted: full | grid | ro)
     TermKit.bind(iframe, {onActive, touchScroll, fontSize, font})   per iframe load: poll window.term, activity events, font size,
                                                               touch-to-wheel shim, overscroll hardening, and (flag only) the
                                                               opt-in JetBrains Mono font -> handle
     TermKit.fontState / TermKit.fontReady                     'off' | 'loading' | 'on' | 'failed', and a promise of the final one
     TermKit.touchScroller({step, threshold, edge, momentum, onWheel})   the shim's accumulator, testable on its own
     TermKit.fitSoon(iframe)                                   debounced term.fit() (resize event when fit() is missing)
     TermKit.viewportFit()                                     --vvh / --vvt from visualViewport (soft keyboard aware)
     TermKit.keyBar(host, {send, sendText, compact})   the 44 px key bar -> {root, setCompact(on), setApproval(on)}
     TermKit.pressable(button, fire, opts) / TermKit.repeater(fire, opts)   press and hold-to-repeat, soft keyboard stays up
     TermKit.contextParts(session) / compactKeys(...) / backTarget(...) / clampFont(...)   small pure helpers
     TermKit.termPane(tmux, {mode, header, drop, modes, onClose, onPopOut, onAddToQuad, ...}) -> {root, update(row, st), setMode(m), reload(), fit(), focus(), destroy()}
                                                               (v0.5.9) one session's terminal with its header, task line and permission line: the dock
                                                               (shell.js) mounts it in full mode; the pure helpers paneLabel / paneParts / paneLine / panePending /
                                                               sizeChip / typingTarget / ctxInfo (the context chip's one reading, dock and quad) are exported
     TermKit.tileMenu(ctx) -> {open(anchorEl, byKeyboard), close(), toggle, update(patch), destroy(), isOpen}      (v0.5.9c) the view dropdown of a quad tile: VIEW / INPUT / TUNE / SESSION
     TermKit.composer(ctx) -> {open(anchorEl), close(), el, update(patch), destroy(), isOpen}                       send a prompt: a popover or sheet, and `el` the docked one-line box
     TermKit.tune(ctx) -> {open(anchorEl, byKeyboard), close(), mount(targetEl), run(cmd, arg), rename(anchorEl), update(patch), destroy(), isOpen}   model, effort, switches, commands
       destroy() on each: the tile is going (close the surface, no timer or send afterwards)
     TermKit.tuneGate / tunePlan / tuneCurrent / tuneRegistry                                                          their pure helpers (the terminal page's gate and chips, for any agent) */
'use strict';

const TermKit = (() => {
  const MODES = ['full', 'grid', 'ro'];
  const RENDERERS = ['canvas', 'dom', 'webgl'];
  const FONT_MIN = 8;
  const FONT_MAX = 28;
  const GRID_FONT = 11;
  const BIND_TIMEOUT_MS = 10000;
  const BIND_POLL_MS = 100;

  /* The opt-in terminal font (v0.5.8, frozen in v0.5.21: OFF by default, kept as is). ttyd's own page asks for system fonts, which differ
     per device (a phone has no Menlo, no Consolas), so cell width, cursor and box drawing vary. bind() can load the vendored JetBrains
     Mono into the iframe instead. No phone or laptop was measured, so the board keeps ttyd's own fonts; ccboard:term:font=1 (or bind's
     `font: true`, or ?font=1 on the page URL) turns it on per browser; see TermKit.fontState and the README, "The terminal page". */
  const FONT_FLAG = 'ccboard:term:font';
  const FONT_FAMILY = 'JetBrains Mono';
  const FONT_URL = '/static/vendor/fonts/jetbrains-mono-latin-wght-normal.woff2';
  const FONT_LOAD_MS = 8000;                    // a stalled download ends as 'failed' instead of 'loading' for ever

  /* ---- pure helpers ------------------------------------------------------------------------------------------------ */

  /* A font size in whole px inside [FONT_MIN, FONT_MAX], or null when `n` is not a number. */
  function clampFont(n) {
    const v = Number(n);
    if (n === null || n === undefined || n === '' || !Number.isFinite(v)) return null;
    return Math.min(FONT_MAX, Math.max(FONT_MIN, Math.round(v)));
  }

  /* The iframe URL for ttyd (tailscale serve maps /tty to ttyd; URL options win over ttyd's own -t defaults).
     full: /tty/?arg=<name>[&fontSize=..]; grid: /tty/?arg=<name>&arg=grid&fontSize=11&rendererType=canvas&disableResizeOverlay=true&disableReconnect=true;
     ro: like full plus &arg=ro. The mode is whitelisted (a bad one throws, so it can never ride into the wrapper's argument list).
     quiet = disableResizeOverlay + disableReconnect (default on for grid, where many small tiles must not flash overlays). */
  function ttyUrl(name, opts) {
    if (typeof name !== 'string' || !name) throw new Error('bad session name');
    const o = opts || {};
    const mode = o.mode === undefined || o.mode === null ? 'full' : o.mode;
    if (!MODES.includes(mode)) throw new Error('bad tty mode');
    const grid = mode === 'grid';
    let q = 'arg=' + encodeURIComponent(name);
    if (mode !== 'full') q += '&arg=' + mode;
    let fs = clampFont(o.fontSize);
    if (fs === null && grid) fs = GRID_FONT;
    if (fs !== null) q += '&fontSize=' + fs;
    let renderer = o.renderer;
    if (renderer !== undefined && renderer !== null && renderer !== '') {
      if (!RENDERERS.includes(renderer)) throw new Error('bad renderer');
    } else renderer = grid ? 'canvas' : null;
    if (renderer) q += '&rendererType=' + renderer;
    const quiet = o.quiet === undefined || o.quiet === null ? grid : !!o.quiet;
    if (quiet) q += '&disableResizeOverlay=true&disableReconnect=true';
    return '/tty/?' + q;
  }

  /* Soft keyboard heuristic: the visual viewport lost 30 % or more of the window. `override` is the persisted choice
     (ccboard:term:keys): 'compact' and 'full' win over the heuristic. */
  function compactKeys(override, vvHeight, innerHeight) {
    if (override === 'compact') return true;
    if (override === 'full') return false;
    const vv = Number(vvHeight);
    const inner = Number(innerHeight);
    return vv > 0 && inner > 0 && vv < 0.7 * inner;
  }

  /* history.back() only when the referrer is this origin (the board opened the terminal); a cold start, a notification tap or
     a shared link has nothing to go back to, so it goes to the Agents page instead. */
  function backTarget(referrer, origin) {
    const r = String(referrer || '');
    const o = String(origin || '');
    if (!o || !r) return 'board';
    return r === o || r.startsWith(o + '/') ? 'back' : 'board';
  }

  /* What the context strip shows for a GET /api/sessions/{name} body (all optional): the task, the last prompt, and the
     question (a pending permission, else the message of a waiting session) or the last message. */
  function contextParts(s) {
    const x = s || {};
    const pend = Array.isArray(x.pending) && x.pending.length ? x.pending[0] : null;
    let ask = '';
    let askKind = '';
    if (pend) {
      ask = pend.summary || pend.tool_name || '';                       // permissions.summarize() already reads 'Bash: <cmd>'
      askKind = 'permission';
    } else if (x.last_message) {
      ask = String(x.last_message);
      askKind = x.state === 'waiting' ? 'waiting' : 'message';
    }
    const task = x.task && typeof x.task === 'object' ? x.task : null;
    return {
      task: task && task.title ? String(task.title).slice(0, 200) : '',
      taskId: task && task.id !== undefined && task.id !== null ? task.id : null,
      phase: task && task.phase ? String(task.phase) : '',
      prompt: x.last_prompt ? String(x.last_prompt).trim().slice(0, 600) : '',
      ask: ask.trim().slice(0, 800),
      askKind,
      approve: !!pend,
    };
  }

  /* ---- hold-to-repeat ---------------------------------------------------------------------------------------------- */

  function defaultTimers() {
    return {
      setTimeout: (f, ms) => setTimeout(f, ms), clearTimeout: (id) => clearTimeout(id),
      setInterval: (f, ms) => setInterval(f, ms), clearInterval: (id) => clearInterval(id),
    };
  }

  /* repeater(fire, {delay: 400, every: 90, max: 20, backlog: true, timers}) -> {down(), up(), cancel()}.
     down() fires once at once; if it stays down for `delay` ms, a tick every `every` ms adds one more. Ticks that arrive while
     the previous fire(n) is still in flight pile up (backlog) and go out together as ONE fire(n) of at most `max`; with
     backlog:false they are dropped instead (scroll buttons must not keep scrolling after the finger has left). up() sends what
     is still queued. fire(n) may return a promise; a rejection is the caller's to report. */
  function repeater(fire, opts) {
    const o = Object.assign({ delay: 400, every: 90, max: 20, backlog: true, coalesce: 250 }, opts || {});
    const T = o.timers || defaultTimers();
    let holdTimer = null;
    let tickTimer = null;
    let flushTimer = null;                                   // repeats wait up to `coalesce` ms so several keys travel in one request
    let queued = 0;
    let inflight = false;
    let isDown = false;

    function flush() {
      if (inflight || queued <= 0) return;
      const n = Math.min(queued, o.max);
      queued -= n;
      inflight = true;
      let r = null;
      try { r = fire(n); } catch (_) { r = null; }
      Promise.resolve(r).catch(() => { /* reported by the caller */ }).then(() => { inflight = false; if (queued > 0) flush(); });
    }
    function stopTimers() {
      if (holdTimer !== null) { T.clearTimeout(holdTimer); holdTimer = null; }
      if (tickTimer !== null) { T.clearInterval(tickTimer); tickTimer = null; }
      if (flushTimer !== null) { T.clearTimeout(flushTimer); flushTimer = null; }
    }
    function flushSoon() {
      if (flushTimer !== null) return;
      flushTimer = T.setTimeout(() => { flushTimer = null; flush(); }, o.coalesce);
    }
    return {
      down() {
        if (isDown) return;
        isDown = true;
        queued += 1;
        flush();
        holdTimer = T.setTimeout(() => {
          holdTimer = null;
          tickTimer = T.setInterval(() => {
            if (!o.backlog && (inflight || queued > 0)) return;
            queued += 1;
            if (!o.backlog || queued >= o.max) flush();   // scroll keys: one request at a time; a full batch goes now
            else flushSoon();                              // typed keys: coalesce (one {keys:[...]} per `coalesce` window)
          }, o.every);
        }, o.delay);
      },
      up() { isDown = false; stopTimers(); flush(); },
      cancel() { isDown = false; stopTimers(); queued = 0; },
      get pending() { return queued; },
    };
  }

  function halt(e) { if (e && e.cancelable !== false && typeof e.preventDefault === 'function') e.preventDefault(); }

  /* pressable(button, fire, {repeat, delay, every, max, backlog, timers}): the button acts on pointerdown, not click, and
     cancels pointerdown / mousedown / touchstart so focus never leaves the composer: the soft keyboard stays up. A click with
     detail 0 (keyboard or assistive technology) fires once. tabindex -1 keeps it out of the tab order. */
  function pressable(btn, fire, opts) {
    const o = opts || {};
    const rep = o.repeat ? repeater(fire, o) : null;
    let pressed = false;
    btn.setAttribute('tabindex', '-1');
    const end = () => {
      if (!pressed) return;
      pressed = false;
      btn.classList.remove('down');
      if (rep) rep.up();
    };
    btn.addEventListener('pointerdown', (e) => {
      if (e.pointerType === 'mouse' && e.button !== undefined && e.button !== 0) return;
      halt(e);
      if (pressed) return;
      pressed = true;
      btn.classList.add('down');
      try { if (typeof btn.setPointerCapture === 'function' && e.pointerId !== undefined) btn.setPointerCapture(e.pointerId); } catch (_) { /* not capturable */ }
      if (rep) rep.down(); else fire(1);
    });
    for (const ev of ['pointerup', 'pointercancel', 'lostpointercapture', 'pointerleave']) btn.addEventListener(ev, end);
    btn.addEventListener('touchstart', halt, { passive: false });
    btn.addEventListener('mousedown', halt);
    btn.addEventListener('contextmenu', halt);
    btn.addEventListener('click', (e) => {
      if (!e.detail) fire(1);            // Enter / Space on a focused button, a screen reader's activate, or button.click(): detail 0
      else halt(e);                      // a real pointer click: pointerdown already acted
    });
    return btn;
  }

  /* ---- touch -> wheel -------------------------------------------------------------------------------------------- */

  /* touchScroller({step: 18, threshold: 8, edge: 24, momentum: 300, onWheel(deltaY, target, at), now, timers}) -> {start(x, y, target),
     move(x, y), end()}. Below `threshold` px of travel nothing happens (a tap stays a tap); from there one onWheel per `step` px of
     travel, deltaY signed like a trackpad (finger down = negative = scroll back in time). A gesture that starts within `edge` px of
     the left edge is ignored (the iOS back swipe). With momentum > 0 a flick keeps emitting for that many ms, slowing down.
     move() returns true once the gesture has engaged (the caller then cancels the touchmove). */
  function touchScroller(opts) {
    const o = Object.assign({ step: 18, threshold: 8, edge: 24, momentum: 300, onWheel() {}, now: () => Date.now() }, opts || {});
    const T = o.timers || defaultTimers();
    let g = null;                         // the live gesture
    let momentumTimer = null;

    function drain(state, target, at) {
      while (Math.abs(state.acc) >= o.step) {
        const dir = state.acc > 0 ? 1 : -1;
        state.acc -= dir * o.step;
        o.onWheel(-dir * o.step, target, at);
      }
    }
    function stopMomentum() { if (momentumTimer !== null) { T.clearInterval(momentumTimer); momentumTimer = null; } }

    return {
      start(x, y, target) {
        stopMomentum();
        if (!(x >= o.edge)) { g = null; return false; }
        g = { x, y, sy: y, lastY: y, acc: 0, engaged: false, target, samples: [{ t: o.now(), y }] };
        return true;
      },
      move(x, y) {
        if (!g) return false;
        if (!g.engaged) {
          if (Math.abs(y - g.sy) < o.threshold) return false;
          g.engaged = true;
          g.acc = y - g.sy;
        } else {
          g.acc += y - g.lastY;
        }
        g.lastY = y;
        g.x = x;
        g.samples.push({ t: o.now(), y });
        if (g.samples.length > 6) g.samples.shift();
        drain(g, g.target, { x, y });
        return true;
      },
      end() {
        const done = g;
        g = null;
        if (!done || !done.engaged || !(o.momentum > 0) || done.samples.length < 2) return false;
        const a = done.samples[0];
        const b = done.samples[done.samples.length - 1];
        const dt = b.t - a.t;
        if (dt <= 0 || dt > 150 || o.now() - b.t > 80) return false;          // the finger rested before it lifted
        const v = (b.y - a.y) / dt;                                            // px per ms, signed
        if (Math.abs(v) < 0.4) return false;
        const tick = 30;
        const coast = { acc: 0 };
        const at = { x: done.x, y: done.lastY };
        let elapsed = 0;
        stopMomentum();
        momentumTimer = T.setInterval(() => {
          elapsed += tick;
          coast.acc += v * Math.max(0, 1 - elapsed / o.momentum) * tick;
          drain(coast, done.target, at);
          if (elapsed >= o.momentum) stopMomentum();
        }, tick);
        return true;
      },
    };
  }

  /* ---- opt-in font state ------------------------------------------------------------------------------------------- */

  let fontState = 'off';                        // 'off' | 'loading' | 'on' | 'failed'
  let fontPromise = null;                       // created per attempt; null = nothing was asked for (fontReady then resolves with the state)
  let fontSeq = 0;

  /* One attempt = one token (one terminal's document). settle(state) is the only way out of 'loading' and takes effect once; the
     exported state and promise follow the latest attempt only, so a page with several terminals (a grid) still gives every one its
     own font while TermKit.fontState reports the newest. A reload or destroy() settles the old attempt as 'off': a slow download then
     never repaints a dead document. The promise never rejects; it resolves with the state at the time it settled. */
  function fontAttempt() {
    const seq = ++fontSeq;
    let resolve = () => {};
    fontPromise = new Promise((res) => { resolve = res; });
    fontState = 'loading';
    let settled = false;
    let cleanup = null;
    return {
      open: () => !settled,
      onSettled(fn) { cleanup = fn; },                                // the load timer is cleared however the attempt ends
      settle(state) {
        if (settled) return;
        settled = true;
        if (seq === fontSeq) fontState = state;
        if (cleanup) { try { cleanup(); } catch (_) { /* a cleanup must not keep the state from settling */ } }
        resolve(seq === fontSeq ? fontState : state);
      },
    };
  }

  /* The persisted switch: ccboard:term:font = '1'. Read when a frame loads (never at load time of this file). A phone has no console, so
     the page URL can flip it: /term/<session>?font=1 turns it on (and keeps it on), ?font=0 turns it off and forgets it. */
  function fontFlag() {
    try {
      const q = new URLSearchParams(location.search || '').get('font');
      if (q === '1' || q === '0') {
        try { if (q === '1') localStorage.setItem(FONT_FLAG, '1'); else localStorage.removeItem(FONT_FLAG); } catch (_) { /* storage may be unavailable: the URL still decides this load */ }
        return q === '1';
      }
    } catch (_) { /* no location */ }
    try { return localStorage.getItem(FONT_FLAG) === '1'; } catch (_) { return false; }
  }

  /* "'JetBrains Mono', <what ttyd had>": prepended once, so a second pass over the same terminal changes nothing. */
  function withFontFamily(prev) {
    const cur = typeof prev === 'string' && prev.trim() ? prev : 'monospace';
    return cur.includes(FONT_FAMILY) ? cur : "'" + FONT_FAMILY + "', " + cur;
  }

  /* The font step itself, for one ttyd window (same origin: the caller already read w.document). Order matters: the face is loaded and added
     to the iframe's document.fonts FIRST, so that when term.options.fontFamily changes xterm measures the cell with the real glyphs and
     the fit() after it gets the final cols and rows. Every step is guarded; any failure ends as 'failed' and leaves the terminal as ttyd
     made it. Never throws, never blocks the caller (the rest of bind() does not wait for the download). */
  function startFont(w, iframe, attempt) {
    let timer = null;
    const done = (state) => attempt.settle(state);
    attempt.onSettled(() => { if (timer !== null) { clearTimeout(timer); timer = null; } });
    try {
      const Face = w.FontFace;
      const fonts = w.document ? w.document.fonts : null;
      if (typeof Face !== 'function' || !fonts || typeof fonts.add !== 'function') { done('failed'); return; }
      const face = new Face(FONT_FAMILY, 'url(' + FONT_URL + ')', { weight: '100 800' });
      timer = setTimeout(() => { timer = null; done('failed'); }, FONT_LOAD_MS);
      Promise.resolve(face.load()).then(() => {
        if (!attempt.open()) return;                                   // superseded, destroyed or timed out meanwhile
        fonts.add(face);
        const t = w.term;
        if (t && t.options) t.options.fontFamily = withFontFamily(t.options.fontFamily);
        fitNow(iframe);
        done('on');
      }).catch(() => done('failed'));
    } catch (_) {
      done('failed');
    }
  }

  /* ---- the iframe -------------------------------------------------------------------------------------------------- */

  const fitTimers = new WeakMap();

  function frameWindow(iframe) {
    try { return iframe && iframe.contentWindow ? iframe.contentWindow : null; } catch (_) { return null; }
  }

  function fitNow(iframe) {
    const w = frameWindow(iframe);
    if (!w) return false;
    try {
      if (w.term && typeof w.term.fit === 'function') { w.term.fit(); return true; }
      w.dispatchEvent(new w.Event('resize'));         // ttyd refits on its window's resize event
      return true;
    } catch (_) { return false; }
  }

  /* A pending debounced fit that must not run any more (the iframe is being taken down). */
  function fitCancel(iframe) {
    const t = fitTimers.get(iframe);
    if (t !== undefined) { clearTimeout(t); fitTimers.delete(iframe); }
  }

  /* Debounced term.fit() (250 ms): layout changes come in bursts (keyboard, rotation, strip toggles). */
  function fitSoon(iframe, delay) {
    if (!iframe) return;
    const prev = fitTimers.get(iframe);
    if (prev !== undefined) clearTimeout(prev);
    fitTimers.set(iframe, setTimeout(() => { fitTimers.delete(iframe); fitNow(iframe); }, delay === undefined ? 250 : delay));
  }

  /* Set the page-wide custom properties from the visual viewport: --vvh (height) and --vvt (offset from the layout viewport
     top, which iOS moves when it scrolls a focused field into view). body.term is sized by them, so the composer sits right
     above the soft keyboard and nothing scrolls. Returns {update(), stop()}. */
  function viewportFit() {
    const root = document.documentElement;
    const vv = window.visualViewport || null;
    const apply = () => {
      const h = Math.floor(vv ? vv.height : window.innerHeight);
      if (h > 0) root.style.setProperty('--vvh', h + 'px');
      root.style.setProperty('--vvt', Math.max(0, Math.floor(vv ? vv.offsetTop : 0)) + 'px');
    };
    apply();
    if (vv) { vv.addEventListener('resize', apply); vv.addEventListener('scroll', apply); }
    window.addEventListener('resize', apply);
    window.addEventListener('orientationchange', apply);
    return {
      update: apply,
      stop() {
        if (vv) { vv.removeEventListener('resize', apply); vv.removeEventListener('scroll', apply); }
        window.removeEventListener('resize', apply);
        window.removeEventListener('orientationchange', apply);
      },
    };
  }

  /* bind(iframe, {onActive, touchScroll: true, fontSize}) -> handle {iframe, win(), term(), setFontSize(n), fit(), destroy()}.
     On every load of the iframe (ttyd page, same origin through the tailnet host) it polls window.term for up to 10 s, then
       - reports activity (capture-phase focusin / mousedown / touchstart / keydown on the iframe document) to onActive(eventName)
       - applies fontSize (term.options.fontSize, then fit())
       - sets touch-action:none on .xterm and overscroll-behavior:none on the document, through the CSSOM
       - installs the touch -> wheel shim on .xterm-screen (tmux mouse mode turns wheel into history scroll)
       - the opt-in font, only when `font` is true (an explicit false wins) or, with `font` left out, ccboard:term:font = '1' in
         localStorage (or ?font=1 on the page URL, ?font=0 to clear): loads JetBrains Mono into the iframe, THEN sets
         term.options.fontFamily and fits (see startFont). The outcome is TermKit.fontState; without FontFace, or when the load
         fails, the terminal keeps the fonts ttyd chose.
     It gives up silently when the frame is cross-origin or never defines term. */
  function bind(iframe, opts) {
    const o = opts || {};
    const handle = { iframe, win: () => frameWindow(iframe), term: () => { const w = frameWindow(iframe); try { return (w && w.term) || null; } catch (_) { return null; } } };
    let pollTimer = null;
    let generation = 0;

    function stopPoll() { if (pollTimer !== null) { clearInterval(pollTimer); pollTimer = null; } }

    function setFontSize(n) {
      const size = clampFont(n);
      const t = handle.term();
      if (size === null || !t) return null;
      try {
        if (t.options) t.options.fontSize = size;
        if (typeof t.fit === 'function') t.fit();
      } catch (_) { return null; }
      return size;
    }
    handle.setFontSize = setFontSize;
    handle.fit = () => fitNow(iframe);
    let fontTry = null;                       // this binding's current font attempt (null: the font is off for this load)
    handle.destroy = () => {
      generation += 1;
      stopPoll();
      iframe.removeEventListener('load', onLoad);
      if (fontTry) { fontTry.settle('off'); fontTry = null; }
    };

    function wheelDispatcher(w, screen) {
      return (deltaY, target, at) => {
        const Wheel = w.WheelEvent || null;
        if (!Wheel) return;
        const a = at || {};
        (target || screen).dispatchEvent(new Wheel('wheel', { deltaY, deltaMode: 0, bubbles: true, cancelable: true, clientX: a.x || 0, clientY: a.y || 0, view: w }));
      };
    }

    function installTouch(w, screen) {
      const sc = touchScroller({ onWheel: wheelDispatcher(w, screen) });
      const touch = (e) => (e.touches && e.touches.length ? e.touches[0] : (e.changedTouches && e.changedTouches[0]) || null);
      screen.addEventListener('touchstart', (e) => {
        if (e.touches && e.touches.length > 1) { sc.end(); return; }      // a pinch is not a scroll
        const t = touch(e);
        if (t) sc.start(t.clientX, t.clientY, e.target);
      }, { passive: true });
      screen.addEventListener('touchmove', (e) => {
        const t = touch(e);
        if (!t || (e.touches && e.touches.length > 1)) return;
        if (sc.move(t.clientX, t.clientY) && e.cancelable) e.preventDefault();   // no page scroll, no emulated mouse events after the swipe
      }, { passive: false });
      const finish = () => { sc.end(); };
      screen.addEventListener('touchend', finish, { passive: true });
      screen.addEventListener('touchcancel', finish, { passive: true });
    }

    function harden(d) {
      try {
        if (d.documentElement) d.documentElement.style.overscrollBehavior = 'none';
        if (d.body) d.body.style.overscrollBehavior = 'none';
        for (const sel of ['.xterm', '.xterm-screen', '.xterm-viewport']) {
          const n = d.querySelector(sel);
          if (n) { n.style.touchAction = 'none'; n.style.overscrollBehavior = 'none'; }
        }
      } catch (_) { /* a hardening failure must never break the terminal */ }
    }

    function listenActive(d) {
      if (typeof o.onActive !== 'function') return;
      for (const ev of ['focusin', 'mousedown', 'touchstart', 'keydown']) {
        d.addEventListener(ev, () => { try { o.onActive(ev); } catch (_) { /* a listener must not break the terminal */ } }, { capture: true, passive: true });
      }
    }

    /* One poll per load: phase A waits for window.term (activity events, font size), phase B for .xterm-screen (hardening and the
       touch shim). Both give up quietly after BIND_TIMEOUT_MS; a frame that went cross-origin ends the poll the same way. */
    function onLoad() {
      generation += 1;
      const gen = generation;
      stopPoll();
      if (fontTry) fontTry.settle('off');                                  // the previous document's attempt is over
      const wantFont = o.font !== undefined && o.font !== null ? !!o.font : fontFlag();
      fontTry = wantFont ? fontAttempt() : null;
      const attempt = fontTry;
      let fontStarted = false;
      const started = Date.now();
      let w = null;
      const step = () => {
        if (gen !== generation) return true;
        try {
          if (!w) {
            const win = frameWindow(iframe);
            if (win && win.term && win.document) {
              w = win;
              listenActive(w.document);
              if (o.fontSize !== undefined && o.fontSize !== null) setFontSize(o.fontSize);
              if (attempt) { fontStarted = true; startFont(w, iframe, attempt); }
            }
          }
          if (w) {
            const screen = w.document.querySelector('.xterm-screen');
            if (screen) {
              harden(w.document);
              if (o.touchScroll !== false) installTouch(w, screen);
              return true;
            }
          }
        } catch (_) { if (attempt && !fontStarted) attempt.settle('failed'); return true; }
        if (Date.now() - started > BIND_TIMEOUT_MS) {
          if (attempt && !fontStarted) attempt.settle('failed');          // window.term never showed up: nothing to put a font into
          if (w) harden(w.document);
          return true;
        }
        return false;
      };
      if (step()) return;
      pollTimer = setInterval(() => { if (step()) stopPoll(); }, BIND_POLL_MS);
    }
    iframe.addEventListener('load', onLoad);
    return handle;
  }

  /* ---- the key bar ------------------------------------------------------------------------------------------------- */

  /* label: visible text; short: label in compact mode; key: tmux key name for send(); title: tooltip and aria-label; repeat: hold-to-repeat.
     Two rows (v0.5.6d): PgUp / PgDn / Top / Bottom are the scroll rail's job (term.js, the right edge of the terminal), so they are not repeated
     here and Ctrl+O joined the arrows. Shift+Tab reads '⇧Tab' at every width: one label, 13 px like the others, the title spells it out. */
  const KEY_ROWS = [
    [
      { label: 'Esc', key: 'Escape', title: 'Escape' },
      { label: 'Tab', key: 'Tab', title: 'Tab' },
      { label: '⇧Tab', key: 'BTab', title: 'Shift+Tab' },
      { label: 'Ctrl-C', short: '^C', key: 'C-c', title: 'Ctrl-C' },
      { label: 'Enter', key: 'Enter', title: 'Enter' },
    ],
    [
      { label: '↑', key: 'Up', title: 'Up', repeat: true },
      { label: '↓', key: 'Down', title: 'Down', repeat: true },
      { label: '←', key: 'Left', title: 'Left', repeat: true },
      { label: '→', key: 'Right', title: 'Right', repeat: true },
      { label: '⌫', key: 'BSpace', title: 'Backspace', repeat: true },
      { label: 'Ctrl+O', key: 'C-o', title: 'Ctrl+O' },
    ],
  ];

  /* keyBar(host, {send(keys[]), sendText(text, enter), compact}) -> {root, setCompact(on), setApproval(on)}.
     Rows: Esc Tab ⇧Tab Ctrl-C Enter / arrows, backspace and Ctrl+O. Every key is at least 44 px (term.css). Holding an arrow or backspace repeats it
     after 400 ms, every 90 ms, batched into one send() of at most 20 keys. Compact (soft keyboard up) keeps Esc ^C Tab ⇧Tab Enter and a More toggle
     for the second row. setApproval(true) adds a y / n row (a pending permission: remote approve is off while this page is open, the answer is typed
     into the terminal). */
  function keyBar(host, opts) {
    const o = opts || {};
    const send = typeof o.send === 'function' ? o.send : () => {};
    const sendText = typeof o.sendText === 'function' ? o.sendText : () => {};
    const shorts = [];

    function keyButton(spec) {
      const b = el('button', { type: 'button', class: 'kb-key', 'aria-label': spec.title || spec.label, title: spec.title || spec.label, 'data-key': spec.key, text: spec.label });
      if (spec.short) shorts.push({ node: b, long: spec.label, short: spec.short });
      pressable(b, (n) => send(new Array(n).fill(spec.key)), spec.repeat ? { repeat: true, delay: 400, every: 90, max: 20 } : {});
      return b;
    }
    function textButton(label, text, title) {
      const b = el('button', { type: 'button', class: 'kb-key kb-ask', 'aria-label': title, title, text: label });
      pressable(b, () => sendText(text, true), {});
      return b;
    }

    const rows = KEY_ROWS.map((row, i) => el('div', { class: 'kb-row kb-r' + (i + 1) }, ...row.map(keyButton)));
    const more = el('button', { type: 'button', class: 'kb-key kb-more', 'aria-expanded': 'false', 'aria-label': 'Show more keys', title: 'More keys', text: 'More' });
    pressable(more, () => {
      const on = root.classList.toggle('more');
      more.setAttribute('aria-expanded', on ? 'true' : 'false');
      more.textContent = on ? 'Less' : 'More';
    }, {});
    rows[0].append(more);
    const ask = el('div', { class: 'kb-row kb-ask-row' }, textButton('y ⏎', 'y', 'Answer y and Enter'), textButton('n ⏎', 'n', 'Answer n and Enter'));
    const root = el('div', { class: 'kb', role: 'toolbar', 'aria-label': 'Terminal keys' }, ask, ...rows);

    function setCompact(on) {
      root.classList.toggle('compact', !!on);
      root.classList.remove('more');
      more.setAttribute('aria-expanded', 'false');
      more.textContent = 'More';
      for (const s of shorts) s.node.textContent = on ? s.short : s.long;
    }
    function setApproval(on) { root.classList.toggle('approval', !!on); }
    setCompact(!!o.compact);
    if (host) host.append(root);
    return { root, setCompact, setApproval };
  }

  /* ---- the pane: one session's terminal with its header (the dock, and the quad tiles that want it) ------------------------------------- */

  const PANE_MODES = ['full', 'grid', 'ro', 'tail'];
  const PANE_FS_KEY = 'ccboard:term:fs';               // the terminal page's text size: a full-mode pane starts with the same one
  const TAIL_LINES = 14;
  const TAIL_NOTE = 'waiting for output…';

  const clip = (text, n) => { const t = String(text === null || text === undefined ? '' : text).replace(/\s+/g, ' ').trim(); return t.length > n ? t.slice(0, n - 1) + '…' : t; };

  /* ['project/repo · ', 'name'] for a ccboard session name: the two halves of paneLabel(), so a narrow header can cut the project/repo and keep the session name whole.
     Anything that is not project--repo--name is ['', the text]. paneLabel(t) === parts.join(''). */
  function paneParts(tmux) {
    const label = paneLabel(tmux);
    const p = String(tmux === null || tmux === undefined ? '' : tmux).split('--');
    if (p.length !== 3) return ['', label];
    const where = p[0] + '/' + (p[1] === 'root' ? 'project folder' : p[1]) + ' · ';
    return [where, label.slice(where.length)];
  }

  /* A header that cut the project/repo of its title down to a sliver ('p') shows the session name alone instead: toggles .off (display:none, pages.css / shell.css) on the
     `where` span when what the flex layout left it is under `min` px (default 36) and it is cut at all; true when it is off. Measured after layout, so call it from a resize
     callback; the class comes off first so the room is the real one. A whole project/repo, however narrow, stays. */
  function fitName(where, min) {
    if (!where || typeof where.getBoundingClientRect !== 'function' || !where.classList) return false;
    where.classList.remove('off');
    const w = where.getBoundingClientRect().width;
    const off = w > 0 && where.scrollWidth > w + 0.5 && w < (typeof min === 'number' ? min : 36);
    where.classList.toggle('off', off);
    where.classList.remove('pend');                                                  // measured once: a quad tile's header may show it now (or has dropped it)
    return off;
  }

  /* The context chip, one reading for the dock pane and the quad tile: null without a number, else {pct, text: 'ctx 61%', level: '' | 'hi' (80 and up) | 'crit' (90 and up),
     title}. The dock paints hi / crit as .warn / .bad, the tile as .hi / .crit. */
  function ctxInfo(pct) {
    if (typeof pct !== 'number' || !Number.isFinite(pct)) return null;
    const n = Math.round(pct);
    return { pct: n, text: 'ctx ' + n + '%', level: n >= 90 ? 'crit' : n >= 80 ? 'hi' : '', title: 'context window used: ' + n + '%' };
  }

  /* 'project/repo · name' for a ccboard session name (project--repo--name; repo root = 'project folder'); anything else is shown as it is. */
  function paneLabel(tmux) {
    const p = String(tmux === null || tmux === undefined ? '' : tmux).split('--');
    if (p.length !== 3) return String(tmux === null || tmux === undefined ? '' : tmux);
    return p[0] + '/' + (p[1] === 'root' ? 'project folder' : p[1]) + ' · ' + p[2];
  }

  /* What a pane's task line says for a session row of the state payload: the question when the session is waiting (its last message), else the task's title,
     else the last prompt. -> {text, kind: 'ask' | 'task' | 'prompt' | '', phase}. */
  function paneLine(row) {
    const r = row && typeof row === 'object' ? row : {};
    const task = r.task && typeof r.task === 'object' && r.task.title ? r.task : null;
    const phase = task && task.phase ? String(task.phase) : '';
    if (r.state === 'waiting' && r.last_message) return { text: clip(r.last_message, 240), kind: 'ask', phase };
    if (task) return { text: clip(task.title, 200), kind: 'task', phase };
    if (r.last_prompt) return { text: clip(r.last_prompt, 240), kind: 'prompt', phase: '' };
    return { text: '', kind: '', phase: '' };
  }

  /* The permission request waiting on a session, or null: the row's own `pending` list (GET /api/sessions/<name>), else state.pending_permissions
     ({id, tmux_name, tool_name, summary}). -> {id, summary}. */
  function panePending(st, tmux, row) {
    let list = row && Array.isArray(row.pending) ? row.pending : null;
    if (!list || !list.length) list = st && Array.isArray(st.pending_permissions) ? st.pending_permissions.filter((p) => p && p.tmux_name === tmux) : [];
    const p = list.find((x) => x && x.id !== undefined && x.id !== null);
    return p ? { id: p.id, summary: String(p.summary || p.tool_name || 'permission request') } : null;
  }

  /* The size chip of a grid / read-only pane: '45x30' (the pane's own cols x rows), or 'cropped' when the session's window (win = [w, h], tmux's) is more than
     two columns or rows larger than what the pane shows. null when the pane has no size yet. */
  function sizeChip(cols, rows, win) {
    const c = Number(cols);
    const r = Number(rows);
    if (!(c > 0 && r > 0)) return null;
    const w = Array.isArray(win) ? Number(win[0]) : 0;
    const h = Array.isArray(win) ? Number(win[1]) : 0;
    const cropped = w > 0 && h > 0 && (w - c > 2 || h - r > 2);
    return { text: cropped ? 'cropped' : c + 'x' + r, cropped, title: 'shows ' + c + 'x' + r + (w > 0 && h > 0 ? ' of the ' + w + 'x' + h + ' window' : '') };
  }

  /* The element that has the keyboard when it is a text field a person types in (input, textarea, select, contenteditable), else null: what a freshly
     mounted terminal must hand focus back to, because ttyd focuses its own terminal when it loads. */
  function typingTarget(doc) {
    try {
      const d = doc || document;
      const a = d.activeElement;
      if (!a || a === d.body) return null;
      if (a.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)) return a;
    } catch (_) { /* no document */ }
    return null;
  }

  /* termPane(tmux, {mode, header, drop, modes, fontSize, renderer, quiet, font, restoreFocus, holdFocus, cls, onClose, onPopOut, onAddToQuad, onMode, onActive, decide})
       -> {root, head, body, name, iframe, pre, mode, update(row, st), setMode(m), reload(), fit(), focus(), destroy()}
     article.tpane[data-tmux][data-mode=full|grid|ro|tail] = a header row (state and agent glyphs, project/repo · session, ctx %, the size chip of a grid / ro
     pane, buttons: reconnect, add to quad, pop out, close), a task line, a pending-permission line (Allow / Deny / In terminal -> POST /api/permission/<id>/<decision>)
     and the body: the ttyd iframe (ttyUrl(tmux, {mode, fontSize, renderer, quiet}), bound with TermKit.bind) or, in tail mode, pre.tail fed by Live.subscribe(tmux,
     fn): one session name per pane, never the whole board.
       header: false     body only (the quad draws its own header, task and permission lines); update() then only keeps the size chip of nothing
       drop: false       the header carries data-drop="session" + data-tmux (dnd.js: a Backlog card dropped on it is handed to the session) unless this is false
       modes: ['grid','full','ro','tail']   a segmented mode control in the header (default: none; setMode() works either way)
       restoreFocus      a node: while nobody has touched the terminal, a focus that ttyd gave its iframe on load goes back to it (the dock never takes a composer's focus)
       holdFocus         true: the terminal never keeps focus it was not asked for (an untouched iframe that got focus on load is blurred, or handed to restoreFocus)
       onClose / onPopOut / onAddToQuad  (tmux) callbacks; a button exists only for a callback (pop out is always a link to /term/<name>, a plain click calls onPopOut)
       decide(id, decision)  replaces the POST (tests, the quad)
     The iframe node lives as long as the pane: a mode change between full / grid / ro changes its src, it is never moved in the DOM; leaving or entering tail drops or
     makes it. update(row, st) is cheap and idempotent: the owner calls it with the session's row of the state payload (null when it is gone) on every render.
     destroy(): the bound handle goes first (no poll on the blank page), then iframe.src = 'about:blank', then the iframe leaves, then the pane; the observers, the
     window listeners, the timers and the Live subscription are released. */
  function termPane(tmux, opts) {
    if (typeof tmux !== 'string' || !tmux) throw new Error('bad session name');
    const o = Object.assign({ mode: 'full', header: true, drop: true }, opts || {});
    let mode = o.mode === undefined || o.mode === null ? 'full' : o.mode;
    if (!PANE_MODES.includes(mode)) throw new Error('bad pane mode');
    ttyUrl(tmux, { mode: 'full' });                                         // the name check, once: a bad name throws here, not on the first reload
    const enc = encodeURIComponent(tmux);
    const label = paneLabel(tmux);
    const timers = new Set();
    const later = (fn, ms) => { if (dead) return 0; const id = setTimeout(() => { timers.delete(id); fn(); }, ms); timers.add(id); return id; };
    let dead = false;
    let touched = false;                                                    // the person used the terminal: focus is theirs from then on
    let ro = null;
    let tailFn = null;
    let tailOff = null;
    let lastRow = null;
    const sig = {};                                                         // what each part of the header shows, so a repaint touches nothing that did not change

    const h = { name: tmux, root: null, head: null, body: null, iframe: null, pre: null, bound: null, guard: null };
    h.root = el('article', { class: 'tpane' + (o.cls ? ' ' + o.cls : ''), 'data-tmux': tmux, 'data-mode': mode });
    h.body = el('div', { class: 'tp-body' });
    const root = h.root;

    /* ---- header ---- */
    let glyphs = null;
    let ctxNode = null;
    let whereNode = null;
    let sizeNode = null;
    let taskNode = null;
    let taskText = null;
    let taskPhase = null;
    let permNode = null;
    let permText = null;
    let permBtns = [];
    let modeBtns = [];
    let reconnectBtn = null;
    let permBusy = false;
    let pendingNow = null;

    const act = (name, icon, title, onClick) => el('button', { class: 'icon minimal tp-btn', type: 'button', 'data-act': name, 'aria-label': title, title, onclick: onClick }, ic(icon));

    function decide(pr, decision) {
      if (permBusy || !pr) return Promise.resolve(false);
      permBusy = true;
      syncPerm();
      const run = typeof o.decide === 'function' ? Promise.resolve().then(() => o.decide(pr.id, decision))
        : Promise.resolve().then(() => api('POST', '/api/permission/' + encodeURIComponent(pr.id) + '/' + decision));
      return run.then(() => true, (e) => { if (typeof toast === 'function') toast((e && e.message) || 'Failed', { kind: 'bad' }); return false; })
        .then((ok) => { permBusy = false; if (!dead) syncPerm(); if (ok && typeof poll === 'function') { try { poll(true); } catch (_) { /* no board poll here */ } } return ok; });
    }

    if (o.header !== false) {
      glyphs = el('span', { class: 'tp-g' });
      ctxNode = el('span', { class: 'tp-ctx hidden' });
      sizeNode = el('span', { class: 'tp-size hidden' });
      const [nWhere, nSess] = paneParts(tmux);                              // two spans, one text: a narrow dock cuts the project/repo and keeps the session name
      whereNode = el('span', { class: 'tp-where', text: nWhere });
      const name = el('span', { class: 'tp-name', title: tmux }, whereNode, el('span', { class: 'tp-sess', text: nSess }));
      if (Array.isArray(o.modes) && o.modes.length) {
        modeBtns = o.modes.filter((m) => PANE_MODES.includes(m)).map((m) => el('button', { class: 'small tp-mode', type: 'button', 'data-mode': m, 'aria-pressed': m === mode ? 'true' : 'false',
          title: m === 'grid' ? 'grid: a cropped view that never resizes the session' : m === 'full' ? 'full: sizes the session to this pane' : m === 'ro' ? 'read only' : 'tail: the last lines as text',
          text: m, onclick: () => setMode(m) }));
      }
      const btns = [];
      reconnectBtn = act('reconnect', 'refresh', 'Reconnect', () => reload());
      btns.push(reconnectBtn);
      if (typeof o.onAddToQuad === 'function') btns.push(act('quad', 'layout-grid', 'Add to the quad view', () => o.onAddToQuad(tmux)));
      const pop = el('a', { class: 'btn icon minimal tp-btn', 'data-act': 'popout', href: '/term/' + enc, target: '_blank', rel: 'noopener', 'aria-label': 'Open in its own window', title: 'Open in its own window' }, ic('share'));
      pop.addEventListener('click', (e) => {
        if (typeof o.onPopOut !== 'function') return;                       // no callback: the link does what a link does
        if (e && (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || (e.button !== undefined && e.button !== 0))) return;
        if (e && typeof e.preventDefault === 'function') e.preventDefault();
        o.onPopOut(tmux);
      });
      btns.push(pop);
      if (typeof o.onClose === 'function') btns.push(act('close', 'cross', 'Close', () => o.onClose(tmux)));
      h.head = el('div', { class: 'tp-head' }, glyphs, name, ctxNode, sizeNode, el('span', { class: 'tp-spacer' }),
        modeBtns.length ? el('div', { class: 'tp-modes', role: 'group', 'aria-label': 'Terminal mode' }, ...modeBtns) : null,
        el('div', { class: 'tp-btns' }, ...btns));
      if (o.drop !== false) {
        h.head.setAttribute('data-drop', 'session');
        h.head.setAttribute('data-tmux', tmux);
        if (typeof Dnd !== 'undefined' && Dnd && typeof Dnd.bind === 'function') Dnd.bind(h.head);
        else if (typeof Lazy !== 'undefined') Lazy.later('dnd', () => { if (typeof Dnd !== 'undefined') Dnd.bind(h.head); });     // dnd.js is lazy (lazy.js)
      }
      taskText = el('span', { class: 'tp-task-text' });
      taskPhase = el('span', { class: 'tp-phase hidden' });
      taskNode = el('div', { class: 'tp-task hidden' }, taskText, taskPhase);
      permText = el('span', { class: 'tp-perm-text' });
      permBtns = [
        el('button', { class: 'primary tinted small tp-allow', type: 'button', 'data-decision': 'allow', text: 'Allow', onclick: () => decide(pendingNow, 'allow') }),
        el('button', { class: 'danger small tp-deny', type: 'button', 'data-decision': 'deny', text: 'Deny', onclick: () => decide(pendingNow, 'deny') }),
        el('button', { class: 'small tp-tui', type: 'button', 'data-decision': 'tui', title: 'Answer it in the terminal: Claude shows its own prompt', text: 'In terminal', onclick: () => decide(pendingNow, 'tui') }),
      ];
      permNode = el('div', { class: 'tp-perm hidden', role: 'group', 'aria-label': 'Permission request' }, permText, el('span', { class: 'tp-perm-btns' }, ...permBtns));
      h.root.append(h.head, taskNode, permNode);
    }
    h.root.append(h.body);

    function syncPerm() {
      if (!permNode) return;
      for (const b of permBtns) b.disabled = permBusy;
    }

    /* ---- body: the iframe, or the tail ---- */
    function frameFont() {
      if (o.fontSize !== undefined && o.fontSize !== null) return o.fontSize;
      if (mode !== 'full') return undefined;
      try { const v = clampFont(localStorage.getItem(PANE_FS_KEY)); return v === null ? undefined : v; } catch (_) { return undefined; }
    }
    const urlFor = (m) => ttyUrl(tmux, { mode: m, fontSize: frameFont(), renderer: o.renderer, quiet: o.quiet });

    /* ttyd focuses its own terminal when its page loads, which moves the keyboard out of whatever the person was typing in. Until the terminal is used
       (a click, a touch or a key inside it), focus that lands on the iframe goes back to restoreFocus, or is released when holdFocus asks for no focus at all. */
    const holds = () => !!o.restoreFocus || !!o.holdFocus;
    function giveBack() {
      const keep = o.restoreFocus;
      if (dead || touched || !h.iframe) return;
      try {
        if (document.activeElement !== h.iframe) return;
        if (keep && keep.isConnected !== false && typeof keep.focus === 'function') keep.focus();
        else if (o.holdFocus && typeof h.iframe.blur === 'function') h.iframe.blur();
      } catch (_) { /* nothing to give back to */ }
    }

    function mountFrame() {
      const f = el('iframe', { class: 'tp-frame', title: 'Terminal ' + label, allow: 'clipboard-write' });
      h.iframe = f;
      h.bound = bind(f, {
        touchScroll: true, fontSize: frameFont(), font: o.font,
        onActive: (ev) => {
          root.classList.add('active');
          if (ev === 'mousedown' || ev === 'touchstart' || ev === 'keydown') touched = true;      // ttyd's own focus() is a focusin: not the person
          if (typeof o.onActive === 'function') o.onActive(ev);
        },
      });
      h.guard = () => { if (holds()) for (const ms of [0, 250, 1000]) later(giveBack, ms); };
      f.addEventListener('load', h.guard);
      f.src = urlFor(mode);
      h.body.append(f);
      if (typeof ResizeObserver === 'function' && !ro) {
        try { ro = new ResizeObserver(() => { fit(); fitHead(); }); ro.observe(h.body); } catch (_) { ro = null; }
      }
    }
    function dropFrame() {
      const f = h.iframe;
      if (!f) return;
      if (h.bound) { try { h.bound.destroy(); } catch (_) { /* already gone */ } h.bound = null; }
      if (h.guard) { f.removeEventListener('load', h.guard); h.guard = null; }
      fitCancel(f);
      try { f.src = 'about:blank'; } catch (_) { /* a frame that cannot navigate is removed all the same */ }
      f.remove();
      h.iframe = null;
    }

    function mountTail() {
      const pre = el('pre', { class: 'tail', text: TAIL_NOTE });
      h.pre = pre;
      h.body.append(pre);
      const live = typeof Live !== 'undefined' ? Live : null;
      if (!live || typeof live.subscribe !== 'function') { pre.textContent = 'the live tail is not available on this page'; return; }
      tailFn = (lines) => {
        pre.textContent = (Array.isArray(lines) ? lines : []).slice(-TAIL_LINES).join('\n') || '(no output yet)';
        pre.scrollTop = pre.scrollHeight;
      };
      const off = live.subscribe(tmux, tailFn);                              // names filter: this one session
      const fn = tailFn;
      tailOff = () => { if (typeof off === 'function') off(); else if (typeof live.unsubscribe === 'function') live.unsubscribe(tmux, fn); };
    }

    function dropTail() {
      if (tailOff) { try { tailOff(); } catch (_) { /* already unsubscribed */ } }
      tailOff = null;
      tailFn = null;
      if (h.pre) { h.pre.remove(); h.pre = null; }
    }

    function fit() { if (h.iframe && !dead) fitSoon(h.iframe); }
    function fitHead() { if (whereNode && !dead) fitName(whereNode); }                // the dock's header shrank: a sliver of project/repo goes

    function reload() {
      if (dead) return false;
      if (mode === 'tail') { dropTail(); mountTail(); return true; }
      if (!h.iframe) return false;
      h.iframe.src = urlFor(mode);                                           // the same address again: ttyd reconnects (grid panes never do by themselves)
      return true;
    }

    function setMode(next) {
      if (dead || !PANE_MODES.includes(next)) return false;
      if (next === mode) return true;
      const wasTail = mode === 'tail';
      mode = next;
      root.setAttribute('data-mode', mode);
      if (next === 'tail') { dropFrame(); mountTail(); }
      else if (wasTail) { dropTail(); mountFrame(); }
      else h.iframe.src = urlFor(mode);                                      // full / grid / ro share the one iframe node
      for (const b of modeBtns) b.setAttribute('aria-pressed', b.getAttribute('data-mode') === mode ? 'true' : 'false');
      if (sizeNode && (mode === 'full' || mode === 'tail')) { sizeNode.classList.add('hidden'); sig.size = ''; }
      if (typeof o.onMode === 'function') o.onMode(mode);
      if (lastRow !== null) update(lastRow.row, lastRow.st);
      return true;
    }

    /* ---- patching ---- */
    function update(row, st) {
      if (dead) return;
      const r = row && typeof row === 'object' ? row : null;
      lastRow = { row: r, st };
      root.classList.toggle('gone', !r);
      if (!h.head) return;
      const state = r ? (r.state || 'unknown') : 'ended';
      const agent = r ? (typeof sessionAgent === 'function' ? sessionAgent(r) : (r.agent || 'claude')) : 'shell';
      const gs = state + '|' + agent;
      if (sig.g !== gs) { sig.g = gs; glyphs.textContent = ''; glyphs.append(stateGlyph(state), agentGlyph(agent)); }
      const stats = r && r.stats && typeof r.stats === 'object' ? r.stats : {};
      const ci = ctxInfo(stats.context_pct);
      const cs = ci ? String(ci.pct) : '';
      if (sig.ctx !== cs) {
        sig.ctx = cs;
        ctxNode.classList.toggle('hidden', !ci);
        ctxNode.classList.toggle('warn', !!ci && ci.level === 'hi');
        ctxNode.classList.toggle('bad', !!ci && ci.level === 'crit');
        ctxNode.textContent = ci ? ci.text : '';
        ctxNode.setAttribute('title', ci ? ci.title : 'context window used');
      }
      let sz = null;
      if (mode === 'grid' || mode === 'ro') {
        const t = h.bound ? h.bound.term() : null;
        sz = t ? sizeChip(t.cols, t.rows, r && r.win) : null;
      }
      const ss = sz ? sz.text : '';
      if (sig.size !== ss) {
        sig.size = ss;
        sizeNode.classList.toggle('hidden', !sz);
        sizeNode.classList.toggle('cropped', !!(sz && sz.cropped));
        sizeNode.textContent = sz ? sz.text : '';
        if (sz) sizeNode.setAttribute('title', sz.title);
      }
      const line = r ? paneLine(r) : { text: 'This session is not running any more.', kind: 'gone', phase: '' };
      const ls = line.kind + '|' + line.text + '|' + line.phase;
      if (sig.line !== ls) {
        sig.line = ls;
        taskNode.classList.toggle('hidden', !line.text);
        taskNode.setAttribute('data-kind', line.kind);
        taskText.textContent = line.kind === 'prompt' ? '› ' + line.text : line.text;
        taskPhase.classList.toggle('hidden', !line.phase);
        taskPhase.textContent = line.phase ? line.phase.replace(/_/g, ' ') : '';
      }
      const pr = r ? panePending(st, tmux, r) : null;
      const ps = pr ? pr.id + '|' + pr.summary : '';
      pendingNow = pr;
      if (sig.perm !== ps) {
        sig.perm = ps;
        permNode.classList.toggle('hidden', !pr);
        permText.textContent = pr ? pr.summary.slice(0, 200) : '';
        permText.setAttribute('title', pr ? pr.summary.slice(0, 800) : '');
      }
    }

    /* ---- focus, observers, teardown ---- */
    function focus() {
      if (dead) return false;
      try {
        const t = h.bound ? h.bound.term() : null;
        if (t && typeof t.focus === 'function') { t.focus(); return true; }
        if (h.iframe && typeof h.iframe.focus === 'function') { h.iframe.focus(); return true; }
      } catch (_) { /* a frame that cannot take focus */ }
      return false;
    }

    const onWindow = () => fit();
    const onVisible = () => { if (!document.hidden) fit(); };
    window.addEventListener('resize', onWindow);
    window.addEventListener('orientationchange', onWindow);
    document.addEventListener('visibilitychange', onVisible);

    function destroy() {
      if (dead) return;
      dead = true;
      for (const id of timers) clearTimeout(id);
      timers.clear();
      window.removeEventListener('resize', onWindow);
      window.removeEventListener('orientationchange', onWindow);
      document.removeEventListener('visibilitychange', onVisible);
      if (ro) { try { ro.disconnect(); } catch (_) { /* already disconnected */ } ro = null; }
      dropFrame();
      dropTail();
      root.remove();
    }

    if (mode === 'tail') mountTail(); else mountFrame();
    Object.defineProperty(h, 'mode', { get: () => mode, enumerable: true });
    Object.assign(h, { update, setMode, reload, fit, focus, destroy });
    return h;
  }

  /* ---- v0.5.9c quad v3: the tile menu, the prompt composer and the tune panel ---------------------------------------------------------
     Three components a quad tile (or any page) opens from its header: tileMenu(ctx) (the view dropdown: VIEW / INPUT / TUNE / SESSION), composer(ctx) (send a
     prompt) and tune(ctx) (model, effort, switches and commands). On a mouse each is a popover under its anchor, on touch a bottom sheet (components.js
     openSheet); the caller says which with ctx.touch. All CSS is termkit.css (classes tk-*). Everything here is built when a factory runs or a surface opens,
     never at load. */

  const TK_MODE_INFO = {
    grid: ['Grid', 'a small tile that does not size the session'],
    full: ['Full', 'a writable terminal that sizes the session'],
    ro: ['Read only', 'watch it, nothing you type reaches the session'],
    tail: ['Tail', 'the last lines of the pane, no terminal'],
  };
  const TK_MODES = ['grid', 'full', 'ro', 'tail'];
  const TK_GATE = 'available when the session is at its prompt';
  const TK_MODELS = ['opus', 'fable', 'sonnet', 'haiku'];                       // Claude's model names; Codex's come from the agent's schema
  const TK_MODEL_HUE = { opus: 'hue-blue', fable: 'hue-violet', sonnet: 'hue-green', haiku: 'hue-slate' };
  const TK_EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max'];
  const TK_CODEX_MODELS = ['gpt-6.1-sol', 'gpt-6-astra', 'gpt-6-sol', 'gpt-6-luna'];   // codex.py FALLBACK_MODELS (launcher.js LX_CODEX_MODELS too): what a Codex tile offers until GET /api/agents lists the box's own
  const TK_CODEX_EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max'];
  const TK_CODEX_REASONING = { 'gpt-6.1-sol': TK_CODEX_EFFORTS.concat(['ultra']) };   // the fallback's per-model extras: ultra only on 6.1-Sol (the box's picker)
  /* Codex's launch flags the Tune changes by restarting the session with the same conversation resumed (issue #2; POST /restart): -a takes on-request | never
     (untrusted is retired and on-failure deprecated: codex --help on 0.160 and 0.161 lists neither) and -s read-only | workspace-write (danger-full-access is a
     bypass: the launcher's acknowledgement, never this panel). The adapter's RESTART_APPROVALS / RESTART_SANDBOXES are the same lists. */
  const TK_CODEX_APPROVALS = ['on-request', 'never'];
  const TK_CODEX_SANDBOXES = ['read-only', 'workspace-write'];
  const TK_RESTART_NOTE = 'Codex has no live command for this: changing it restarts the session and resumes the same conversation';
  /* Ultracode is a setting, not an effort level (Claude Code docs; V8 on 2.1.290): /effort ultracode on and /effort ultracode off, session only, through POST /tune
     with the setting `ultracode` (the pane's "Ultracode on" / "Ultracode off" line is the read-back) */
  const TK_ULTRA_ON = 'on';
  const TK_ULTRA_OFF = 'off';
  const TK_SAVES_DEFAULT = 'Also saves your default for new sessions';         // Claude's /model <name> typed inline (V8): the picker's list is version-specific, so Model stays inline and says so
  const TK_UNVERIFIED = 'Unverified key path: the board reads the result back from the screen';
  /* #84: the words of the auto-continue switch are core.js's (AUTO_CONTINUE_*); these are the fallbacks for a page that loaded the kit without it */
  const TK_AUTO_WHAT = typeof AUTO_CONTINUE_WHAT === 'string' ? AUTO_CONTINUE_WHAT : 'Types continue once after a limit reset or an account switch, and after a reboot if the session was working. This session only.';
  const TK_AUTO_SUB = typeof AUTO_CONTINUE_SUB === 'string' ? AUTO_CONTINUE_SUB : 'after a limit reset or a reboot, this session only';
  const TK_AUTO_BOARD_OFF = typeof AUTO_CONTINUE_BOARD_OFF === 'string' ? AUTO_CONTINUE_BOARD_OFF : 'Off for the whole board (CCBOARD_AUTO_CONTINUE=0), so this switch changes nothing until the board setting is on again.';
  const tkNoAuto = (c) => { const f = c && c.session && c.session.flags; return !!(f && typeof f === 'object' && f.no_autoresume); };      // the session row's flags.no_autoresume
  const tkAutoLabel = (off) => 'Auto-continue: ' + (off ? 'off' : 'on');
  const TK_PENDING_MS = 20000;                                                  // how long a typed setting waits for the statusline that confirms it
  const TK_ESC_GAP_MS = 150;                                                    // Escape and the next keys must not arrive together (a TUI reads ESC + key as Alt + key)
  const TK_CELLS = ['compact', 'context', 'usage', 'cost', 'status', 'rename']; // the command cells, in this order
  const TK_CELL_TITLE = {
    compact: 'Summarise the conversation to free up context', context: 'Show how the context window is used', usage: 'Show plan usage',
    cost: 'Show what this session has cost', status: 'Show the session status', rename: 'Give the session a new name',
  };
  const tkSlash = (cmd, label, arg, read, more) => ({ cmd: '/' + cmd, label, arg, read, destructive: false, verified: true, weight: 0, drive: 'inline', tune: '',
    saves_default: false, choices: null, dialog: false, ...(more || {}) });
  /* The registry until GET /api/agents says otherwise: the SlashSpec rows of claude.py / codex.py (the terminal page's strip reads the same rows, weights included) */
  const TK_SLASH = {
    claude: {
      clear: tkSlash('clear', 'Clear', false, false, { weight: 155, destructive: true }), compact: tkSlash('compact', 'Compact', false, false, { weight: 120 }),
      usage: tkSlash('usage', 'Usage', false, true, { weight: 100, dialog: true }),
      effort: tkSlash('effort', 'Effort', true, false, { weight: 58, tune: 'effort', saves_default: true, tune_verified: false }),
      model: tkSlash('model', 'Model', true, false, { weight: 39, saves_default: true }), rename: tkSlash('rename', 'Rename', true, false, { weight: 10 }),
      context: tkSlash('context', 'Context', false, true, { weight: 9 }), status: tkSlash('status', 'Status', false, true, { weight: 3, dialog: true }),
      cost: tkSlash('cost', 'Cost', false, true, { dialog: true }), fast: tkSlash('fast', 'Fast', true, false, { choices: ['on', 'off'] }),
    },
    codex: {
      model: tkSlash('model', 'Model', false, false, { drive: 'picker', tune: 'model' }),
      reasoning: { ...tkSlash('x', 'Reasoning', false, false, { drive: 'restart' }), cmd: '-c model_reasoning_effort' },
      approvals: { ...tkSlash('x', 'Approvals', false, false, { drive: 'restart', choices: TK_CODEX_APPROVALS }), cmd: '-a' },
      sandbox: { ...tkSlash('x', 'Sandbox', false, false, { drive: 'restart', choices: TK_CODEX_SANDBOXES }), cmd: '-s' },
      status: tkSlash('status', 'Status', false, true),
    },
  };

  const tkPlain = (v) => (v && typeof v === 'object' ? v : null);
  const tkAgent = (c) => {
    const a = c.agent || (c.session && (typeof sessionAgent === 'function' ? sessionAgent(c.session) : c.session.agent)) || 'claude';
    return String(a);
  };
  const tkStats = (c) => tkPlain(c.stats) || (c.session && tkPlain(c.session.stats)) || {};
  const tkLabel = (c) => paneLabel(c.tmux);
  const tkSession = (c) => '/api/sessions/' + encodeURIComponent(c.tmux);

  /* {show, enabled, title} for a session row (the terminal page's gate): nothing to tune on a shell or an unknown row; enabled only when the prompt is free: idle,
     done, errored or waiting on the idle prompt, no compaction and no permission request open. An explicit ctx.atPrompt (true | false) wins over the row. */
  function tuneGate(row, atPrompt) {
    const r = tkPlain(row);
    const shown = !!(r && r.agent && r.agent !== 'shell');
    if (typeof atPrompt === 'boolean') return { show: shown || !!r, enabled: atPrompt, title: atPrompt ? '' : TK_GATE };
    if (!shown) return { show: false, enabled: false, title: '' };
    const flags = tkPlain(r.flags) || {};
    const idle = r.state === 'idle' || r.state === 'done' || r.state === 'errored' || (r.state === 'waiting' && flags.wait_kind === 'idle');
    const ok = idle && !flags.compacting && !(Array.isArray(r.pending) && r.pending.length);
    return { show: true, enabled: ok, title: ok ? '' : TK_GATE };
  }

  /* The slash registry for an agent: the schema's (GET /api/agents) when it has one, else the built-in rows. Shell: none. */
  function tuneRegistry(agent, schema) {
    const s = tkPlain(schema) && tkPlain(schema.slash);
    if (s && Object.keys(s).length) return s;
    return ownKey(TK_SLASH, agent) ? TK_SLASH[agent] : {};
  }

  /* What the tune panel offers one agent: {model: {cmd, options}, effort: {cmd, options}, fast, ultra, cells}. Claude: opus fable sonnet haiku, low..max, the Fast
     and Ultracode switches (when /fast and /effort exist) and the command cells; Codex: the schema's models and its reasoning levels, else the built-in lists (the state's agent entry has neither; the command is /reasoning there,
     not /effort), no switches. An option is {value, label, arg}. */
  /* A group is {cmd, options, via, setting, note, unverified}: `via` 'tune' = POST /tune {setting, value} (the agent's picker, this session only), 'command' = POST
     /command {cmd, arg} (typed inline), 'restart' = POST /restart {<cmd>: value} (a launch flag: Codex's reasoning, approval and sandbox; the session restarts with the
     conversation resumed). A registry row with `tune` (v0.5.21) goes through /tune; an older server's row (no `tune`) keeps the inline path. `note`: what the person must
     know (Claude's /model typed inline also saves the default; a restart says so). `unverified`: the box check did not run that exact key path. */
  function tunePlan(agent, schema, stats) {
    const reg = tuneRegistry(agent, schema);
    const sc = tkPlain(schema) || {};
    const has = (k) => ownKey(reg, k);
    const row = (k) => tkPlain(reg[k]) || {};
    const opt = (v) => ({ value: String(v), label: String(v), arg: String(v) });
    const group = (k, cmd, options) => {
      const r = row(k);
      const tune = typeof r.tune === 'string' && r.tune ? r.tune : '';
      const proven = r.tune_verified === undefined || r.tune_verified === null ? r.verified : r.tune_verified;   // the picker path's own box check (pickers.py PROVEN)
      if (r.drive === 'restart') return { cmd, options, via: 'restart', setting: cmd, note: TK_RESTART_NOTE, unverified: false };
      return { cmd, options, via: tune ? 'tune' : 'command', setting: tune, note: !tune && r.saves_default ? TK_SAVES_DEFAULT : '', unverified: proven === false && !!tune };
    };
    const plan = { model: null, effort: null, approvals: null, sandbox: null, fast: false, ultra: false, cells: [] };
    if (agent === 'claude') {
      if (has('model')) plan.model = group('model', 'model', TK_MODELS.map(opt));
      if (has('effort')) {
        plan.effort = group('effort', 'effort', TK_EFFORTS.map(opt));
        plan.ultra = true;
      }
      plan.fast = has('fast');
    } else if (agent === 'codex') {
      const listed = Array.isArray(sc.models) ? sc.models.filter((m) => typeof m === 'string' && m) : [];
      const models = (listed.length ? listed : TK_CODEX_MODELS).slice(0, 12);       // a state row's agent entry (status_summary) has no models: the built-in list stands in
      const pickers = row('model').drive === 'picker';                              // an older server typed /model <slug> inline, which Codex sends to the model: no row then
      if (has('model') && pickers && models.length) plan.model = group('model', 'model', models.map(opt));
      const by = tkPlain(sc.reasoning_by_model) || (listed.length ? {} : TK_CODEX_REASONING);
      const cur = tuneModel(stats, plan.model || { options: models.map(opt) });
      /* The levels of the CURRENT model (reasoning_by_model). While the model is not known, only the levels every listed model accepts: the schema's `efforts` is the
         union over the catalogue and would offer ultra (GPT-6.1 Sol only) to a model that stops at max, which Codex then refuses. */
      const known = models.some((m) => Array.isArray(by[m]) && by[m].length);
      const lists = known ? models.map((m) => (Array.isArray(by[m]) && by[m].length ? by[m] : TK_CODEX_EFFORTS)) : [];   // a model with no entry has the base levels
      const shared = lists.length ? lists[0].filter((e) => lists.every((l) => l.includes(e))) : [];
      const named = Array.isArray(sc.efforts) ? sc.efforts.filter((e) => typeof e === 'string' && e) : [];
      const unknown = lists.length ? shared : (named.length ? named : TK_CODEX_EFFORTS);     // no per-model levels at all: the schema's own list
      const levels = (cur && Array.isArray(by[cur]) && by[cur].length ? by[cur] : (cur ? TK_CODEX_EFFORTS : unknown)).filter((e) => typeof e === 'string' && e);
      if (has('reasoning') && row('reasoning').drive === 'restart' && levels.length) plan.effort = group('reasoning', 'reasoning', levels.map(opt));
      if (has('approvals') && row('approvals').drive === 'restart') plan.approvals = group('approvals', 'approval', TK_CODEX_APPROVALS.map(opt));
      if (has('sandbox') && row('sandbox').drive === 'restart') plan.sandbox = group('sandbox', 'sandbox', TK_CODEX_SANDBOXES.map(opt));
    }
    for (const k of TK_CELLS) {
      if (!has(k) || row(k).drive === 'picker' || row(k).drive === 'restart') continue;
      plan.cells.push({ key: k, cmd: String(row(k).cmd || '/' + k), read: !!row(k).read, arg: !!row(k).arg, dialog: row(k).dialog === undefined ? !!row(k).read : !!row(k).dialog });
    }
    return plan;
  }

  /* The current model option's value: the longest option name the statusline's model (display name or id) contains ('Opus 5' -> opus, 'gpt-5.5-mini' beats 'gpt-5.5'). */
  function tuneModel(stats, group) {
    const st = tkPlain(stats) || {};
    const hay = [st.model, st.model_id].filter(Boolean).join(' ').toLowerCase();
    if (!hay || !group) return '';
    let best = '';
    for (const o of group.options) { const v = o.value.toLowerCase(); if (hay.includes(v) && v.length > best.length) best = o.value; }
    return best;
  }

  /* One reading of the session for the panel: {model, effort, fast, ultra, approval, sandbox}. effort also reads Codex's `reasoning`. Nothing is guessed: fast is true | false only
     when the statusline reported it (null = unknown, no settled On or Off); ultra ('on' | 'off') comes from what the board last read back from the pane after a /tune
     (flags.tuned: no statusline or rollout field carries it), else '' (unknown, nothing selected). Codex's approval and sandbox are the rollout's own words (stats.approval,
     stats.sandbox: the newest turn_context), else the ones the board started the session with (flags.perm); a word the panel does not offer (granular, danger-full-access)
     selects nothing. */
  function tuneCurrent(stats, plan, flags) {
    const st = tkPlain(stats) || {};
    const fl = tkPlain(flags) || {};
    const tuned = tkPlain(fl.tuned) || {};
    const perm = tkPlain(fl.perm) || {};
    const seen = (k) => { const t = tkPlain(tuned[k]); return t && typeof t.value === 'string' ? t.value : ''; };
    const canon = (v) => String(v === undefined || v === null ? '' : v).toLowerCase().replace(/[^a-z]/g, '');
    const word = (v, offered) => { const c = canon(v); return (c && offered.find((o) => canon(o) === c)) || ''; };
    const eff = String(st.effort || st.reasoning || '').toLowerCase();
    return { model: tuneModel(st, plan && plan.model), effort: eff, fast: typeof st.fast === 'boolean' ? st.fast : null, ultra: seen('ultracode'),
      approval: word(st.approval, TK_CODEX_APPROVALS) || word(perm.approval, TK_CODEX_APPROVALS), sandbox: word(st.sandbox, TK_CODEX_SANDBOXES) || word(perm.sandbox, TK_CODEX_SANDBOXES) };
  }

  /* What one change sends: {path, body} under /api/sessions/<tmux>. A /tune group sends {setting, value}; an inline one {cmd, arg}. The terminal page's strip and this
     panel both build their requests here, so a fix to one reaches both. */
  function tuneRequest(group, value) {
    if (group && group.via === 'restart') return { path: '/restart', body: { [group.setting]: String(value) } };
    if (group && group.via === 'tune') return { path: '/tune', body: { setting: group.setting, value: String(value) } };
    return { path: '/command', body: { cmd: group && group.cmd, arg: String(value) } };
  }

  /* 409 {error, message, state, wait_kind, retry}: the board will not type into the pane right now; anything else says what it says. */
  const tkLast = { text: '', at: 0 };
  function tkSay(text, kind) {                                                // the same words at the speed of a held key: once per 3 s
    const now = Date.now();
    if (text === tkLast.text && now - tkLast.at < 3000) return;
    tkLast.text = text;
    tkLast.at = now;
    if (typeof toast === 'function') toast(text, { kind: kind || 'bad' });
  }
  function tkRefused(e, what) {
    const b = tkPlain(e && e.body);
    if (e && e.status === 409 && b) tkSay((b.message || b.error || 'refused') + (typeof b.retry === 'number' ? ' · try again in ' + b.retry + ' s' : ''), 'warn');
    else if (e && e.status === 404 && !(b && b.error)) tkSay('This board is too old for ' + what + ': update ccboard');
    else tkSay((e && e.message) || 'Failed');
  }
  const tkKeys = (c, keys) => api('POST', tkSession(c) + '/keys', { keys }).catch(() => null);
  const tkPoll = () => { try { if (typeof poll === 'function') poll(true); } catch (_) { /* no board poll on this page */ } };
  const tkSleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  /* A restart-driven change (Codex's reasoning, approvals and sandbox): ask the board for the exact command first (POST /restart {<setting>: value, preview: true}: the
     adapter builds it, the browser assembles nothing), show it in a popover under `anchor` (a bottom sheet on touch) with Restart and Cancel, and only Restart sends the
     change. Both the Tune panel and the terminal page's strip call this one function. A refusal (409: the session is working, has no conversation yet, ...) is a toast
     through tkSay and nothing changes. Resolves true when the session was restarted. ctx = {tmux, touch, agent, onRestart(res)}; `beforeOpen` runs once the preview is in. */
  async function tuneRestart(ctx, group, value, anchor, beforeOpen) {
    const c = ctx || {};
    const req = tuneRequest(group, value);
    const label = String((group && group.setting) || 'setting');
    let pre = null;
    try { pre = await api('POST', tkSession(c) + req.path, { ...req.body, preview: true }); } catch (e) { tkRefused(e, 'restarting with other launch flags'); tkPoll(); return false; }
    if (typeof beforeOpen === 'function') beforeOpen();                          // the Tune panel steps aside only once there is a command to show
    return new Promise((resolve) => {
      let done = false;
      const finish = (ok) => { if (done) return; done = true; resolve(ok); };
      const S = surface({ onClose: () => finish(false) });
      const err = el('p', { class: 'tk-err bad', role: 'alert' });
      const cancel = el('button', { type: 'button', class: 'tk-cancel minimal', text: 'Cancel', onclick: () => S.close(true) });
      const go = el('button', { type: 'button', class: 'tk-send', text: 'Restart', onclick: async () => {
        err.textContent = '';
        cancel.disabled = true;
        go.disabled = true;
        let res = null;
        try { res = await api('POST', tkSession(c) + req.path, req.body); } catch (e) {
          const b = tkPlain(e && e.body);
          err.textContent = (b && (b.message || b.error)) || (e && e.message) || 'The restart failed';
          cancel.disabled = false;
          go.disabled = false;
          tkPoll();
          return;
        }
        finish(true);
        S.close(false);
        tkSay('Restarted with ' + label + ' ' + String(value) + '. If the terminal says it exited, reload it.', 'ok');
        if (typeof c.onRestart === 'function') { try { c.onRestart(res); } catch (e) { console.error('ccboard tune onRestart', e); } }
        tkPoll();
      } });
      const body = el('div', { class: 'tk-tune tk-restart' + (c.touch ? ' tk-touch' : ''), 'data-agent': tkAgent(c) },
        el('div', { class: 'tk-gl', text: 'RESTART WITH ' + label.toUpperCase() + ' ' + String(value).toUpperCase() }),
        el('p', { class: 'tk-note', text: TK_RESTART_NOTE + '. Anything the session is typing stops. This is the command that will run:' }),
        el('pre', { class: 'tk-pre', text: String((pre && pre.cmd) || '') }),
        err,
        el('div', { class: 'tk-actions' }, cancel, go));
      if (!(pre && pre.cmd)) {                                                   // no command to show (an old board, the demo): never offer a restart nobody can read first
        err.textContent = 'The board did not say which command would run, so the restart is off. Reload the page, or update ccboard.';
        go.disabled = true;
      }
      if (!S.open(anchor || null, () => body, { touch: !!c.touch, title: 'Restart · ' + paneLabel(c.tmux), cls: 'tk-pop-tune', width: 320 })) finish(false);
    });
  }

  /* ---- surface: a popover under an anchor (mouse) or a bottom sheet (touch) ------------------------------------------------------------
     surface({onClose, onKey(e), escape(e) -> true when it used the Esc, closeOnTab}) -> {open(anchor, build, {touch, title, width, cls, bare}), close(refocus), isOpen, root, body}.
     The popover goes into the anchor's own fullscreen element when it is not <html> (the page's fullscreen is: its body is inside it), else into the anchor's dialog, else the body.
     It flips above the anchor when it does not fit below, slides up over it when neither side has the room, and takes the room it has (max-height, scrolling inside) so nothing is cut off. Outside pointerdown, Esc, a route change and a
     fullscreen change close it; a window resize puts it under its anchor again (and closes it when the anchor is gone). A sheet is components.js openSheet (placement bottom); when there is no #sheet on the page it falls back to the popover. */
  function surface(opts) {
    const o = opts || {};
    let pop = null;
    let sheetBody = null;
    let anchor = null;
    let live = false;
    let unlisten = null;
    let width = 0;
    const ctl = {};

    /* The page goes fullscreen on <html> (quad.js): the body is inside it, themed (.bp5-dark), so a popover goes there. Only a fullscreen element of its own (a tile, a
       dialog) takes it in: outside <body> nothing is themed (Times, a filled white Send, a filled red Kill). */
    const hostFor = (a) => {
      const fs = document.fullscreenElement || document.webkitFullscreenElement || null;
      if (fs && fs !== document.documentElement && a && typeof fs.contains === 'function' && fs.contains(a)) return fs;
      return (a && typeof a.closest === 'function' && a.closest('dialog')) || document.body;
    };

    function place(node, a, width) {
      const r = a && typeof a.getBoundingClientRect === 'function' ? a.getBoundingClientRect() : null;
      if (!r) return;
      const vw = Number(window.innerWidth) || 0;
      const vh = Number(window.innerHeight) || 0;
      const w = Number(node.offsetWidth) || width || 0;
      let left = Math.round(r.left);
      if (vw && w) left = Math.min(left, vw - w - 8);
      node.style.left = Math.max(8, left) + 'px';
      const h = Number(node.offsetHeight) || 0;
      let top = Math.round(r.bottom + 4);
      let max = 0;
      if (vh) {
        const below = vh - top - 8;
        const above = Math.round(r.top - 4 - 8);
        if (!h || h <= below) max = below;                                       // it fits under the anchor
        else if (h <= above) { top = Math.round(r.top - 4 - h); max = above; }   // no room below: above the anchor
        else { top = Math.max(8, vh - h - 8); max = vh - 16; }                   // neither side has it: slide up until it is whole (over the anchor), and scroll only when the window itself is too short
      }
      node.style.top = top + 'px';
      if (max > 0) node.style.maxHeight = max + 'px';
    }

    function listen() {
      const onDoc = (e) => {
        const t = e && e.target;
        if (pop && typeof pop.contains === 'function' && pop.contains(t)) return;
        if (anchor && typeof anchor.contains === 'function' && anchor.contains(t)) return;     // the anchor's own click toggles
        ctl.close(false);
      };
      const onKey = (e) => {
        if (e.key === 'Escape') {
          e.preventDefault();
          e.stopPropagation();
          if (typeof o.escape === 'function' && o.escape(e)) return;             // the content used it (a rename form, a readout)
          ctl.close(true);
        } else if (e.key === 'Tab' && o.closeOnTab) ctl.close(false);
        else if (typeof o.onKey === 'function') o.onKey(e);
      };
      const shut = () => ctl.close(false);
      const again = () => {                                                       // the window changed size: stay under the anchor, or go when the anchor is gone
        if (!pop) return;
        if (anchor && anchor.isConnected === false) { ctl.close(false); return; }
        pop.style.maxHeight = '';
        place(pop, anchor, width);
      };
      document.addEventListener('pointerdown', onDoc, true);
      document.addEventListener('keydown', onKey, true);
      document.addEventListener('fullscreenchange', shut);
      window.addEventListener('hashchange', shut);
      window.addEventListener('resize', again);
      unlisten = () => {
        document.removeEventListener('pointerdown', onDoc, true);
        document.removeEventListener('keydown', onKey, true);
        document.removeEventListener('fullscreenchange', shut);
        window.removeEventListener('hashchange', shut);
        window.removeEventListener('resize', again);
      };
    }

    ctl.open = (anchorEl, build, p) => {
      const x = p || {};
      if (live) return false;
      anchor = anchorEl || null;
      const body = build();
      if (x.touch && typeof openSheet === 'function') {
        const sh = openSheet({ title: x.title || '', body, placement: 'bottom', onClose: () => { if (live) ctl.close(false); } });
        if (sh) { sheetBody = body; live = true; return true; }
      }
      pop = el('div', { class: 'tk-pop' + (x.cls ? ' ' + x.cls : ''), tabindex: '-1', role: x.bare ? 'presentation' : 'dialog', 'aria-label': x.bare ? null : (x.title || null) }, body);
      hostFor(anchor).append(pop);
      width = x.width || 0;
      place(pop, anchor, width);
      listen();
      live = true;
      return true;
    };

    ctl.close = (refocus) => {
      if (!live) return;
      live = false;
      if (unlisten) { unlisten(); unlisten = null; }
      const a = anchor;
      const p = pop;
      const s = sheetBody;
      pop = null;
      sheetBody = null;
      anchor = null;
      if (p) p.remove();
      if (s) {                                                                    // close the sheet only while it still shows this body (a later openSheet swapped it in place)
        const dlg = document.getElementById('sheet');
        if (dlg && typeof dlg.contains === 'function' && dlg.contains(s) && typeof closeSheet === 'function') closeSheet();
      }
      if (typeof o.onClose === 'function') o.onClose();
      if (refocus && a && typeof a.focus === 'function') { try { a.focus(); } catch (_) { /* nothing to focus */ } }
    };
    Object.defineProperties(ctl, {
      isOpen: { get: () => live },
      root: { get: () => pop },
      body: { get: () => sheetBody },
    });
    return ctl;
  }

  /* ---- tileMenu ---------------------------------------------------------------------------------------------------------------------- */

  /* tileMenu(ctx) -> {open(anchorEl, byKeyboard), close(), toggle, isOpen, update(patch), root}
     ctx = {tmux, session, agent, mode, modes, touch, actions: {setMode(m), zoom, fullscreenTile, popout, openTerm, dock | null, reload, keysHere, allow, deny, tui, composer,
     tune, compact, context, usage, rename, close, kill, dockComposer?}, perm: {pending, summary}, atPrompt, why, zoomed?, composerDocked?, keysTarget?}.
     Four labelled groups: VIEW (the four modes as one segmented row with the current one tinted, Zoom, Fullscreen this tile, Pop out, Open in terminal, Add to dock, Reload),
     INPUT (Send a prompt…, Show composer, Keys here, and Allow / Deny / In terminal while a permission is pending), TUNE (Tune…, /compact, /context: off with `why` while the
     session is not at its prompt; /usage, /cost and Rename… live inside Tune…; a shell has no TUNE group; a Codex session only Tune…), SESSION (Close tile, then Kill session,
     red-outlined, filled only when armed, two taps through confirmButton). The whole menu is about 540 px tall with a mouse (it fits 1280x800 with room), and is capped to the window.
     An item whose action is null is left out, an empty group too. open() on an open menu closes it (a second tap on the ▾). Opened by a pointer nothing is highlighted; by
     the keyboard the first item has the focus. update(patch) merges into ctx and repaints an open menu in place. */
  function makeTileMenu(ctx) {
    const c = ctx || {};
    const killKey = () => 'tile-kill:' + c.tmux;
    let body = null;
    let touchNow = false;
    const disarm = () => { if (typeof ui !== 'undefined' && ui && ui.confirm === killKey()) ui.confirm = null; };
    const S = surface({
      closeOnTab: true,
      onClose: () => { disarm(); body = null; },
      onKey: (e) => {
        const root = body;
        if (!root) return;
        const cells = Array.from(root.querySelectorAll('.tk-mode'));                  // the mode row is one stop for Up / Down (the current mode), Left / Right move inside it
        const stop = cells.find((n) => n.classList.contains('on')) || cells[0] || null;
        const rows = Array.from(root.querySelectorAll('.tk-item, .tk-mode, .tk-kill button')).filter((n) => !n.classList.contains('tk-mode') || n === stop);
        if (!rows.length) return;
        const at = document.activeElement;
        const ci = cells.indexOf(at);
        if (ci >= 0 && (e.key === 'ArrowRight' || e.key === 'ArrowLeft')) { e.preventDefault(); cells[(ci + (e.key === 'ArrowRight' ? 1 : -1) + cells.length) % cells.length].focus(); return; }
        const i = ci >= 0 ? rows.indexOf(stop) : rows.indexOf(at);
        const go = (n) => { e.preventDefault(); rows[(n + rows.length) % rows.length].focus(); };
        if (e.key === 'ArrowDown') go(i + 1);
        else if (e.key === 'ArrowUp') go(i < 0 ? rows.length - 1 : i - 1);
        else if (e.key === 'Home') go(0);
        else if (e.key === 'End') go(rows.length - 1);
        else if (typeof e.key === 'string' && e.key.length === 1 && e.key !== ' ' && !e.ctrlKey && !e.metaKey && !e.altKey) e.stopPropagation();   // an open menu keeps the page's letter shortcuts (F, Z ...) to itself
      },
    });

    /* what the menu says for the current ctx: [{key, label, note?, items: [{id, kind, label, title?, sub?, checked?, off?, act}]}] */
    function groups() {
      const a = tkPlain(c.actions) || {};
      const agent = tkAgent(c);
      const gate = tuneGate(c.session, c.atPrompt);
      const atPrompt = typeof c.atPrompt === 'boolean' ? c.atPrompt : gate.enabled;
      const why = c.why || TK_GATE;
      const add = (list, id, label, act, x) => { if (typeof act === 'function') list.push(Object.assign({ id, kind: 'item', label, act }, x || {})); };

      const view = [];
      const modes = (Array.isArray(c.modes) && c.modes.length ? c.modes : TK_MODES).filter((m) => ownKey(TK_MODE_INFO, m));
      if (typeof a.setMode === 'function' && modes.length) {                       // one segmented row: Grid | Full | Read only | Tail
        view.push({ id: 'modes', kind: 'segment', label: 'View mode', cells: modes.map((m) => ({ id: 'mode:' + m, label: TK_MODE_INFO[m][0], checked: c.mode === m, title: TK_MODE_INFO[m][0] + ': ' + TK_MODE_INFO[m][1], sub: TK_MODE_INFO[m][1], act: () => a.setMode(m) })) });
      }
      add(view, 'zoom', c.zoomed ? 'Back to the grid' : 'Zoom', a.zoom, { title: c.zoomed ? 'Show every tile again' : 'Let this tile fill the whole grid' });
      add(view, 'fullscreen', 'Fullscreen this tile', a.fullscreenTile, { title: 'Zoom this tile and fill the screen' });
      add(view, 'popout', 'Pop out', a.popout, { title: 'Open the terminal in its own window' });
      add(view, 'term', 'Open in terminal', a.openTerm, { title: 'Open the terminal page' });
      add(view, 'dock', 'Add to dock', a.dock, { title: 'Keep this session in the dock beside the board' });
      add(view, 'reload', 'Reload', a.reload, { title: 'Connect the terminal again' });

      const input = [];
      add(input, 'composer', 'Send a prompt…', a.composer, { title: 'Type a prompt and send it to this session' });
      add(input, 'dockcomposer', 'Show composer', a.dockComposer, { kind: 'check', checked: !!c.composerDocked, title: 'A one-line prompt box at the bottom of this tile' });
      add(input, 'keys', 'Keys here', a.keysHere, { kind: typeof c.keysTarget === 'boolean' ? 'check' : 'item', checked: !!c.keysTarget, title: 'The key bar types into this tile' });
      const perm = tkPlain(c.perm);
      if (perm && perm.pending) {
        if (typeof a.allow === 'function' || typeof a.deny === 'function' || typeof a.tui === 'function') input.push({ id: 'permnote', kind: 'note', label: 'Permission: ' + String(perm.summary || 'permission request') });
        add(input, 'allow', 'Allow', a.allow);
        add(input, 'deny', 'Deny', a.deny);
        add(input, 'tui', 'In terminal', a.tui, { title: 'Let the agent show its own prompt in the terminal' });
      }

      const tune = [];
      let tuneHint = '';
      if (agent !== 'shell') {
        const reg = tuneRegistry(agent, c.schema);
        const off = atPrompt ? {} : { off: true };
        add(tune, 'tune', 'Tune…', a.tune, Object.assign({ title: 'Model, effort and commands' }, off));
        for (const [k, label, act] of [['compact', '/compact', a.compact], ['context', '/context', a.context]]) {          // /usage, /cost and Rename… are one tap further, inside Tune…
          if (ownKey(reg, k)) add(tune, k, label, act, Object.assign({ title: TK_CELL_TITLE[k] }, off));
        }
        for (const it of tune) if (it.off) { it.why = why; it.title = why; }
        try {                                                                     // v0.5.21: say where the rows that moved went, and only the ones Tune… really has for this agent
          const keys = tunePlan(agent, c.schema).cells.map((x) => x.key);
          const moved = [keys.includes('usage') ? 'Usage' : '', keys.includes('rename') ? 'rename' : ''].filter(Boolean);
          if (moved.length) tuneHint = moved.join(' and ') + (moved.length > 1 ? ' are' : ' is') + ' inside Tune…';
        } catch (_) { /* no hint */ }
      }

      const session = [];
      if (typeof a.autoContinue === 'function' && agent !== 'shell') {          // #84: a board switch, not a typed command: never held back by the prompt gate
        const off = tkNoAuto(c);
        const boardOff = typeof boardAutoContinueOff === 'function' && boardAutoContinueOff();
        add(session, 'autocontinue', tkAutoLabel(off), a.autoContinue, { kind: 'check', checked: !off, sub: boardOff ? 'off for the whole board (CCBOARD_AUTO_CONTINUE=0)' : TK_AUTO_SUB,
          title: TK_AUTO_WHAT + (boardOff ? ' ' + TK_AUTO_BOARD_OFF : '') });
      }
      add(session, 'close', 'Close tile', a.close, { title: 'Take this session out of the quad (it keeps running)' });
      const out = [
        { key: 'view', label: 'VIEW', items: view },
        { key: 'input', label: 'INPUT', items: input },
        { key: 'tune', label: 'TUNE', items: tune, note: tune.length && !atPrompt ? why : tuneHint },
        { key: 'session', label: 'SESSION', items: session, kill: typeof a.kill === 'function' },
      ];
      return out.filter((g) => g.items.some((it) => it.kind !== 'note') || g.kill);
    }

    function pick(it) {
      if (it.off) { if (typeof toast === 'function') toast(it.why, { kind: 'info' }); return; }
      choose(it.act);
    }
    /* A popover goes first, then the action (it may open a popover of its own); a sheet goes after it: an action that opens another sheet swaps this one in place, and
       closing first would let the close event of this one wipe the new one. */
    function choose(act) {
      if (touchNow) { try { act(); } finally { S.close(false); } return; }
      S.close(false);
      act();
    }

    function killRow(kill) {
      const wrap = el('div', { class: 'tk-kill' });
      const fill = () => {
        wrap.textContent = '';
        wrap.append(confirmButton(killKey(), 'Kill session', async () => { S.close(false); await kill(); }));
      };
      wrap.addEventListener('click', (e) => {                                     // confirmButton repaints the page, not this popover: the arm / cancel shows here
        fill();
        const b = e && e.detail === 0 ? wrap.querySelector('button') : null;
        if (b) b.focus();
      });
      fill();
      return wrap;
    }

    /* the four modes: one row of equal cells, the current one tinted (no tick: the tint and aria-checked say it); on touch the current one is described below, where a mouse has tooltips */
    function modeRow(it, touch) {
      const row = el('div', { class: 'tk-modes', role: 'group', 'aria-label': it.label, 'data-id': it.id });
      const cur = it.cells.find((x) => x.checked);
      for (const x of it.cells) {
        row.append(el('div', { class: 'tk-mode' + (x.checked ? ' on' : ''), role: 'menuitemradio', tabindex: '-1', 'data-id': x.id, title: x.title || null, 'aria-checked': x.checked ? 'true' : 'false',
          onclick: () => pick(x), onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(x); } } },
          el('span', { class: 'tk-name', text: x.label })));
      }
      if (!touch || !cur) return row;
      return el('div', { class: 'tk-modewrap' }, row, el('div', { class: 'tk-note tk-modenote', text: cur.label + ': ' + cur.sub }));
    }

    function itemRow(it, touch) {
      if (it.kind === 'segment') return modeRow(it, touch);
      const role = it.kind === 'radio' ? 'menuitemradio' : it.kind === 'check' ? 'menuitemcheckbox' : 'menuitem';
      const attrs = { class: 'menuitem tk-item' + (it.off ? ' tk-off' : '') + (it.checked ? ' on' : ''), role, tabindex: '-1', 'data-id': it.id,
        title: it.title || null, 'aria-disabled': it.off ? 'true' : null, 'aria-checked': it.kind === 'item' ? null : (it.checked ? 'true' : 'false'),
        onclick: () => pick(it), onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(it); } } };
      return el('div', attrs,
        el('span', { class: 'tk-tick', 'aria-hidden': 'true' }, it.checked ? ic('tick') : null),
        el('span', { class: 'mi-text tk-label' }, el('span', { class: 'tk-name', text: it.label }), touch && it.sub ? el('span', { class: 'tk-sub', text: it.sub }) : null));
    }

    function build() {
      touchNow = !!c.touch;
      const root = el('div', { class: 'tk-menu' + (touchNow ? ' tk-touch' : ''), role: 'menu', 'aria-label': 'Tile menu: ' + tkLabel(c) });
      for (const g of groups()) {
        const id = 'tk-g-' + g.key + '-' + (Math.random().toString(36).slice(2, 7));
        const sec = el('div', { class: 'tk-group', role: 'group', 'aria-labelledby': id, 'data-group': g.key },
          el('div', { class: 'tk-gl', id, text: g.label }));
        if (g.key === 'tune' && g.note) sec.append(el('div', { class: 'tk-note', text: g.note }));
        for (const it of g.items) sec.append(it.kind === 'note' ? el('div', { class: 'tk-note tk-perm', text: it.label, title: it.label }) : itemRow(it, touchNow));
        if (g.kill) sec.append(killRow(tkPlain(c.actions).kill));
        root.append(sec);
      }
      body = root;
      return root;
    }

    function open(anchorEl, byKeyboard) {
      if (S.isOpen) { S.close(false); return; }
      disarm();
      const touch = !!c.touch;
      S.open(anchorEl, build, { touch, title: 'Tile menu · ' + tkLabel(c), cls: 'tk-pop-menu', width: 260, bare: true });
      const first = body && (body.querySelector('.tk-mode.on') || body.querySelector('.tk-item, .tk-mode'));       // the keyboard starts on the current mode, the first row of the menu
      if (byKeyboard && first) first.focus();
      else if (S.root && typeof S.root.focus === 'function') S.root.focus();      // a pointer highlights nothing: the popover itself holds the focus
    }

    /* an open menu repaints in place; focus stays on the same row when it was on one */
    function update(patch) {
      Object.assign(c, patch || {});
      if (!S.isOpen || !body) return;
      const rows = Array.from(body.querySelectorAll('.tk-item, .tk-mode'));
      const at = rows.indexOf(document.activeElement);
      const old = body;
      const fresh = build();
      while (old.firstChild) old.removeChild(old.firstChild);
      for (const k of Array.from(fresh.children)) old.append(k);
      body = old;
      if (at >= 0) { const next = old.querySelectorAll('.tk-item, .tk-mode')[at]; if (next) next.focus(); }
    }

    return { open, close: (refocus) => S.close(!!refocus), toggle: open, update, destroy: () => S.close(false), get isOpen() { return S.isOpen; }, get root() { return S.root || S.body; } };
  }

  /* ---- composer ---------------------------------------------------------------------------------------------------------------------- */

  /* composer(ctx) -> {open(anchorEl), close(), el, update(patch), isOpen, root}
     ctx = {tmux, session, agent, touch, onSent(kind)}: the prompt box of one session. open() shows it under the anchor (a popover) or as a bottom sheet (touch): a
     two-row box (16 px on touch), the quick replies of ccboard:quick:<tmux> (the agent's defaults when the person has not edited them), a bordered Send, Enter sends,
     Shift+Enter adds a line, Esc closes. `el` is the one-line variant for docking at the bottom of a tile (a row: a one-row box that does not grow, a … button that opens the popover above, and a bordered Send; no chips; it stays after a send). A send goes to
     POST /api/sessions/<tmux>/prompt {text, enter: true, queue}: queue is true only for a Claude session that is working (Codex and a shell never queue); a shell, which has
     no agent row to paste into, gets POST /keys {text, enter: true} instead (as the terminal page does). The toast says "sent" or "queued", onSent(kind) hears the same,
     and the popover or sheet closes. A refusal (409) toasts the server's own words and keeps the text. */
  function makeComposer(ctx) {
    const c = ctx || {};
    let busy = false;
    const parts = [];                                                           // the boxes built: the popover body (kept, so a draft survives a close) and the docked one
    let boxPart = null;
    let dead = false;
    const S = surface({});
    const queueing = () => tkAgent(c) === 'claude' && !!c.session && c.session.state === 'working';

    async function deliver(text) {
      const enc = tkSession(c);
      if (tkAgent(c) === 'shell') { await api('POST', enc + '/keys', { text, enter: true }); return 'sent'; }
      const once = async (queue, retried) => {
        try {
          const r = await api('POST', enc + '/prompt', { text, enter: true, queue });
          return r && r.queued ? 'queued' : 'sent';
        } catch (e) {
          if (e && e.status === 404 && !(e.body && e.body.error)) { await api('POST', enc + '/keys', { text, enter: true }); return 'sent'; }   // an older server has no /prompt
          if (e && e.status === 409 && e.body && e.body.error === 'working' && !queue && !retried && tkAgent(c) === 'claude') return once(true, true);   // a turn began since the last poll
          throw e;
        }
      };
      return once(queueing(), false);
    }

    function syncBusy() {
      for (const p of parts) {
        p.send.disabled = busy;
        for (const b of p.chips.querySelectorAll('button')) b.disabled = busy;
        p.root.classList.toggle('busy', busy);
      }
    }

    /* the text goes out; true when it did (the caller clears its box and closes) */
    async function send(text) {
      const body = String(text === null || text === undefined ? '' : text).replace(/\s+$/, '');
      if (!body.trim() || busy || dead) return false;
      busy = true;
      syncBusy();
      let kind = null;
      try { kind = await deliver(body); } catch (e) { tkRefused(e, 'sending'); }
      busy = false;
      syncBusy();
      if (!kind) return false;
      if (typeof toast === 'function') toast(kind, { kind: 'ok' });
      if (typeof c.onSent === 'function') { try { c.onSent(kind); } catch (e) { console.error('ccboard composer onSent', e); } }
      tkPoll();
      return true;
    }

    function chipsFor(p) {
      p.chips.textContent = '';
      let list = [];
      try { list = typeof quickLoad === 'function' ? quickLoad(c.tmux, tkAgent(c)) : []; } catch (_) { list = []; }     // #69: the agent's own defaults
      for (const text of list) {
        const go = () => send(text).then((ok) => { if (ok && !p.docked) S.close(false); });
        const edit = () => {
          S.close(false);
          if (typeof quickReplyEditor === 'function') quickReplyEditor({ items: typeof quickLoad === 'function' ? quickLoad(c.tmux, tkAgent(c)) : [],
            defaults: typeof quickDefaults === 'function' ? quickDefaults(tkAgent(c)) : undefined,
            onSave: (items) => { if (typeof quickSave === 'function') quickSave(c.tmux, items, tkAgent(c)); } });
        };
        const chip = typeof quickChip === 'function' ? quickChip(text, { cls: 'tk-chip', onSend: go, onEdit: edit })
          : el('button', { type: 'button', class: 'tk-chip', title: 'tap to send', text, onclick: go });
        chip.disabled = busy;
        p.chips.append(chip);
      }
    }

    function build(docked) {
      const ta = el('textarea', { class: 'tk-ta', rows: docked ? '1' : '2', placeholder: docked ? 'Send a prompt…' : 'Type a prompt', 'aria-label': 'Prompt for ' + tkLabel(c),
        autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false', enterkeyhint: 'send' });
      const sendBtn = el('button', { type: 'button', class: 'tk-send', title: 'Send (Enter)', text: 'Send' });
      const p = { ta, send: sendBtn, chips: el('div', { class: 'tk-chips' }), hint: el('span', { class: 'tk-hint' }), root: null, docked };
      const go = () => send(ta.value).then((ok) => {
        if (!ok) return;
        ta.value = '';
        if (typeof composerGrow === 'function') composerGrow(ta);
        if (!docked) S.close(false);
      });
      sendBtn.addEventListener('click', go);
      if (docked) {                                                               // one row that never grows (composerBind would): Enter sends, the … button opens the roomy box
        ta.classList.add('composer');
        ta.setAttribute('title', 'Enter sends. This one-line box has no new line: the … button opens a bigger one.');
        ta.addEventListener('keydown', (e) => {
          if (e.key !== 'Enter' || e.isComposing) return;                               // Enter that confirms an IME candidate is not a send
          e.preventDefault();                                                           // Shift+Enter would add a line this box cannot show
          if (!e.shiftKey && !e.altKey) go();
        });
      } else if (typeof composerBind === 'function') composerBind(ta, { onSend: go, maxRows: 6 });
      else ta.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); go(); } });
      if (docked) {
        const more = el('button', { type: 'button', class: 'tk-more', title: 'More: quick replies and a bigger box', 'aria-label': 'More prompt options', text: '…', onclick: () => open(more) });
        p.root = el('div', { class: 'tk-composer tk-docked', role: 'group', 'aria-label': 'Prompt box' }, ta, more, sendBtn);
      } else {
        const nl = c.touch && typeof newlineButton === 'function' ? newlineButton(ta) : null;
        p.root = el('div', { class: 'tk-composer' + (c.touch ? ' tk-touch' : ''), role: 'group', 'aria-label': 'Send a prompt' },
          el('div', { class: 'tk-gl', text: 'SEND A PROMPT' }), ta, p.chips,
          el('div', { class: 'tk-actions' }, p.hint, nl, sendBtn));
      }
      parts.push(p);
      return p;
    }

    function hintFor(p) { setText(p.hint, queueing() ? 'Working now: your prompt goes in the queue' : (c.touch ? '' : 'Enter sends · Shift+Enter adds a line')); }

    const docked = build(true);
    hintFor(docked);

    function open(anchorEl) {
      if (S.isOpen) { S.close(false); return; }
      const p = boxPart || (boxPart = build(false));
      chipsFor(p);
      hintFor(p);
      S.open(anchorEl, () => p.root, { touch: !!c.touch, title: 'Send a prompt · ' + tkLabel(c), cls: 'tk-pop-composer', width: 420 });
      if (!c.touch) { try { p.ta.focus(); } catch (_) { /* nothing to focus */ } }              // a phone never opens its soft keyboard by itself
    }

    function update(patch) {
      Object.assign(c, patch || {});
      for (const p of parts) hintFor(p);
    }

    /* the tile is going: close the popover or sheet, take the docked box out of the page, and send nothing from now on */
    function destroy() {
      dead = true;
      S.close(false);
      docked.root.remove();
      parts.length = 0;
      boxPart = null;
    }

    return { open, close: (refocus) => S.close(!!refocus), el: docked.root, update, destroy, get isOpen() { return S.isOpen; }, get root() { return S.root || S.body; } };
  }

  /* ---- tune -------------------------------------------------------------------------------------------------------------------------- */

  /* tune(ctx) -> {open(anchorEl, byKeyboard), close(), mount(targetEl), run(cmd, arg, {anchor}), rename(anchorEl), update(patch), isOpen}
     ctx = {tmux, session, agent, stats, touch, schema}: schema is the agent's GET /api/agents entry (state.agents[agent]) when the page has one. The panel has five parts:
     MODEL (a segmented control: opus fable sonnet haiku for Claude, the schema's models for Codex), EFFORT (low medium high xhigh max; Codex's reasoning levels),
     OPTIONS (Fast and Ultracode, toggles: Ultracode is `/effort ultracode on`, and off goes back to high), COMMANDS (an equal-cell grid: /compact /context /usage /cost
     /status /rename…) and a readout for what a read command printed. The current values come from ctx.stats. Every change is POST /api/sessions/<tmux>/command {cmd, arg?};
     a 409 shows as a toast with the server's words and every row is off while the session is not at its prompt (a note says so). A read command leaves the agent's own
     dialog open in the pane: the readout's Close (or closing the panel) sends it one Escape. open() shows the panel under the anchor (a 320 px popover) or as a bottom
     sheet (touch); mount(targetEl) puts the same panel into a page (the terminal page) -> {root, update(patch), destroy()}. run() is the same call without a panel: a
     tile menu's /compact, /context and /usage items use it; a read command's output opens the panel at `anchor`. */
  function makeTune(ctx) {
    const c = ctx || {};
    const T = { busy: false, pend: null, pendTimer: 0, ok: null, readout: null, dialog: false, renaming: false, anchor: null, dead: false, seen: {}, auto: null };
    const panels = new Set();
    const S = surface({
      escape: () => {
        if (T.renaming) { T.renaming = false; syncAll(); return true; }
        if (T.readout) { closeReadout(); return true; }
        return false;
      },
      onClose: () => { for (const p of Array.from(panels)) if (p.owned) { panels.delete(p); } T.renaming = false; closeReadout(); },
    });
    const gateNow = () => tuneGate(c.session, c.atPrompt);
    const planNow = () => tunePlan(tkAgent(c), c.schema, tkStats(c));
    const tkFlags = () => tkPlain(c.flags) || (c.session && tkPlain(c.session.flags)) || {};
    /* the reading, with what a /tune just read back from the pane standing in until the session's own reading moves (Codex's rollout reports a model change at the
       next turn; flags.tuned arrives with the next poll) */
    function currentNow(plan) {
      const cur = tuneCurrent(tkStats(c), plan, tkFlags());
      for (const k of Object.keys(T.seen)) {
        const s = T.seen[k];
        if (cur[k] !== s.before) delete T.seen[k];
        else cur[k] = s.value;
      }
      return cur;
    }
    function flashOk(key) { T.ok = key; setTimeout(() => { if (T.ok === key) { T.ok = null; syncAll(); } }, 1500); }

    /* a read command (/usage, /context, /status, /cost) leaves the agent's dialog in the pane: one Escape closes it (two would open the rewind menu) */
    function settleEscape() {
      if (!T.dialog) return Promise.resolve();
      T.dialog = false;
      return tkKeys(c, ['Escape']).then(() => tkSleep(TK_ESC_GAP_MS));
    }

    function closeReadout() {
      if (!T.readout) return;
      T.readout = null;
      syncAll();
      settleEscape();
    }

    function clearPending(ok) {
      const p = T.pend;
      if (!p) return;
      T.pend = null;
      clearTimeout(T.pendTimer);
      T.pendTimer = 0;
      if (ok) flashOk(p.key);
      else tkSay('/' + p.cmd + (p.arg ? ' ' + p.arg : '') + ' not confirmed by the session', 'warn');
    }

    /* the typed setting waits for the statusline: pending until the stats agree, an ok flash then; 20 s without that says so once */
    function startPending(cmd, arg, key, satisfied) {
      if (T.dead) return;
      clearTimeout(T.pendTimer);
      T.pend = { cmd, arg, key, satisfied };
      T.pendTimer = setTimeout(() => { T.pendTimer = 0; clearPending(false); syncAll(); }, TK_PENDING_MS);
    }

    async function run(cmd, arg, o) {
      const x = o || {};
      const gate = gateNow();
      if (!gate.show) return false;
      if (!gate.enabled) { if (typeof toast === 'function') toast(TK_GATE.charAt(0).toUpperCase() + TK_GATE.slice(1), { kind: 'info' }); return false; }
      if (T.busy) return false;
      if (x.anchor) T.anchor = x.anchor;
      T.busy = true;
      syncAll();
      try {
        await settleEscape();
        const body = { cmd };
        if (arg) body.arg = arg;
        const req = x.tune ? { path: '/tune', body: x.tune } : { path: '/command', body };
        let res = null;
        try { res = await api('POST', tkSession(c) + req.path, req.body); } catch (e) { tkRefused(e, 'tuning'); tkPoll(); return false; }
        if (res && typeof res.screen === 'string') {
          T.dialog = res.dialog !== false;                                         // a read command printed inline (Codex /status, Claude /context) leaves nothing to Escape
          T.readout = { cmd: '/' + cmd, text: String(res.screen).replace(/\s+$/, '') || '(nothing printed)' };
          if (!S.isOpen && !panels.size && !T.dead) openPanel(T.anchor, false);
        } else if (x.tune && res && res.confirmed === true) {                      // read back from the pane: applied, not merely typed
          if (x.pend) { T.seen[x.pend.kind] = { value: x.pend.value, before: currentNow(planNow())[x.pend.kind] }; flashOk(x.pend.key); }
        } else if (x.tune && res && res.confirmed === false) {
          tkSay(res.message || (x.tune.setting + ' not confirmed by the session'), 'warn');
        } else if (x.pend) startPending(x.tune ? x.tune.setting : cmd, x.tune ? x.tune.value : (arg || ''), x.pend.key, x.pend.satisfied);
        if (typeof c.onCommand === 'function') { try { c.onCommand(cmd, arg || '', res); } catch (e) { console.error('ccboard tune onCommand', e); } }
        tkPoll();
        return true;
      } finally {
        T.busy = false;
        syncAll();
      }
    }

    /* ---- one panel ---- */
    function newPanel(variant) {
      const touch = !!c.touch;
      const p = { variant, sig: '', root: el('div', { class: 'tk-tune' + (touch ? ' tk-touch' : ''), 'data-agent': tkAgent(c) }), nodes: [], owned: variant !== 'mount' };
      p.gate = el('p', { class: 'tk-gate hidden', role: 'note' });
      p.readout = el('section', { class: 'tk-readout hidden', 'aria-label': 'Command output' });
      p.rename = null;
      return p;
    }

    const sec = (label, ...kids) => el('section', { class: 'tk-sec' }, el('div', { class: 'tk-gl', text: label }), ...kids);

    /* A restart-driven option (Codex's reasoning, approvals, sandbox): the same gate as a typed command, then TermKit.tuneRestart (preview, confirm, restart). The panel
       closes first, so the confirm has the anchor to itself; T.busy keeps the rows off until it is answered. */
    async function restartRun(group, op, node) {
      const gate = gateNow();
      if (!gate.show) return false;
      if (!gate.enabled) { if (typeof toast === 'function') toast(TK_GATE.charAt(0).toUpperCase() + TK_GATE.slice(1), { kind: 'info' }); return false; }
      if (T.busy) return false;
      const anchor = T.anchor || node || null;
      T.busy = true;
      syncAll();
      try { return await tuneRestart(c, group, op.arg, anchor, () => { if (S.isOpen) S.close(false); }); } finally { T.busy = false; syncAll(); }
    }

    function seg(p, label, group, kind) {
      const long = group.options.some((o) => o.label.length > 8);                // a long name (a Codex model) gets half the track, many short ones a third
      const wrap = el('div', { class: 'tk-seg' + (long ? ' tk-wrap2' : group.options.length > 5 ? ' tk-wrap3' : ''), role: 'group', 'aria-label': label, 'data-kind': kind });
      for (const op of group.options) {
        const want = kind === 'effort' ? op.value.toLowerCase() : op.value;
        const pend = { key: kind + ':' + op.value, kind, value: want, satisfied: (cur) => cur[kind] === want };
        const req = tuneRequest(group, op.arg);
        const b = el('button', { type: 'button', class: 'tk-opt' + (kind === 'model' && ownKey(TK_MODEL_HUE, op.value) ? ' tk-model ' + TK_MODEL_HUE[op.value] : ''), 'data-cmd': group.cmd, 'data-arg': op.arg,
          'data-via': group.via, 'aria-pressed': 'false',
          title: (group.via === 'tune' ? label + ' ' + op.label + ' for this session only' : group.via === 'restart' ? 'Restart with ' + label.toLowerCase() + ' ' + op.label : '/' + group.cmd + ' ' + op.arg)
            + (group.note ? ' (' + group.note.toLowerCase() + ')' : ''), text: op.label,
          onclick: () => (group.via === 'restart' ? restartRun(group, op, b) : req.path === '/tune' ? run(group.cmd, '', { tune: req.body, pend }) : run(group.cmd, op.arg, { pend })) });
        p.nodes.push({ node: b, kind, value: op.value });
        wrap.append(b);
      }
      return wrap;
    }

    /* Fast: `/fast on` or `/fast off` (bare /fast opens a dialog that swallows keys); an unknown state sends on. Ultracode: /tune ultracode on | off (an older
       server: /effort ultracode on | off typed inline); the pane's read-back, not a guess, settles it. */
    function toggle(p, kind, label, title) {
      const state = el('span', { class: 'tk-state', text: 'unknown' });
      const b = el('button', { type: 'button', class: 'tk-tog', 'data-kind': kind, 'aria-pressed': 'false', title, onclick: () => {
        const plan = planNow();
        const cur = currentNow(plan);
        if (kind === 'fast') {
          const want = cur.fast === true ? 'off' : 'on';
          run('fast', want, { pend: { key: 'fast', kind: 'fast', value: want === 'on', satisfied: (now) => now.fast === (want === 'on') } });
          return;
        }
        const want = cur.ultra === TK_ULTRA_ON ? TK_ULTRA_OFF : TK_ULTRA_ON;
        const pend = { key: 'ultra', kind: 'ultra', value: want, satisfied: (now) => now.ultra === want };
        if (plan.effort && plan.effort.via === 'tune') run('effort', '', { tune: { setting: 'ultracode', value: want }, pend });
        else run('effort', 'ultracode ' + want, { pend });
      } }, el('span', { class: 'tk-tn', text: label }), state);
      p.nodes.push({ node: b, kind, state });
      return b;
    }

    /* #84 Auto-continue: a board switch (POST /api/sessions/<tmux>/flags), not a typed command, so the prompt gate and the busy flag of the picker do not hold it
       back. Optimistic: T.auto holds the value just asked for until the session row the page passes in agrees (sync clears it), a refusal puts the old one back. */
    const autoOff = () => (T.auto !== null ? T.auto : tkNoAuto(c));
    function autoToggle(p) {
      const state = el('span', { class: 'tk-state', text: 'on' });
      const b = el('button', { type: 'button', class: 'tk-auto', 'data-kind': 'auto', 'aria-pressed': 'true', title: TK_AUTO_WHAT, onclick: () => {
        if (typeof setAutoContinue !== 'function') return;
        setAutoContinue(c.tmux, !autoOff(), { apply: (off) => { T.auto = off; syncAll(); } });
      } }, el('span', { class: 'tk-tn', text: 'Auto-continue' }), state);
      p.auto = { node: b, state, note: el('p', { class: 'tk-note', text: '' }) };
      return b;
    }

    function cell(p, spec) {
      const isRename = spec.key === 'rename';
      const b = el('button', { type: 'button', class: 'tk-cell', 'data-cmd': spec.key, title: TK_CELL_TITLE[spec.key] + (spec.read ? ' (shows what it prints)' : ''),
        text: isRename ? spec.cmd + '…' : spec.cmd,
        onclick: () => { if (isRename) openRename(); else run(spec.key, '', {}); } });
      p.nodes.push({ node: b, kind: 'cell', value: spec.key });
      return b;
    }

    function renameForm(p) {
      const input = el('input', { type: 'text', class: 'tk-input', maxlength: '120', autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false', enterkeyhint: 'done', 'aria-label': 'New session name', placeholder: 'session name' });
      const err = el('p', { class: 'tk-err bad', role: 'alert' });
      const form = el('form', { class: 'tk-rename hidden', onsubmit: (e) => {
        e.preventDefault();
        const v = input.value.trim();
        if (!v) { err.textContent = 'Type a name first.'; input.setAttribute('aria-invalid', 'true'); try { input.focus(); } catch (_) { /* no focus */ } return; }
        T.renaming = false;
        syncAll();
        run('rename', v, {});
      } },
        el('div', { class: 'tk-gl', text: 'RENAME THE SESSION' }), input, err,
        el('div', { class: 'tk-actions' },
          el('button', { type: 'button', class: 'tk-cancel minimal', text: 'Cancel', onclick: () => { T.renaming = false; syncAll(); } }),
          el('button', { type: 'submit', class: 'tk-send', text: 'Rename' })));
      input.addEventListener('input', () => { err.textContent = ''; input.removeAttribute('aria-invalid'); });
      p.rename = { form, input, err };
      return form;
    }

    const autoShown = () => !!c.tmux && tkAgent(c) !== 'shell' && typeof setAutoContinue === 'function';
    function fill(p, plan) {
      p.root.textContent = '';
      p.nodes = [];
      p.rename = null;
      p.auto = null;
      p.root.append(p.gate);
      const notes = (g) => [g.note ? el('p', { class: 'tk-note', text: g.note }) : null, g.unverified ? el('p', { class: 'tk-note tk-unverified', text: TK_UNVERIFIED }) : null].filter(Boolean);
      const codex = tkAgent(c) === 'codex';
      if (plan.model) p.root.append(sec('MODEL', seg(p, 'Model', plan.model, 'model'), ...notes(plan.model)));
      if (plan.effort) p.root.append(sec(codex ? 'REASONING' : 'EFFORT', seg(p, codex ? 'Reasoning' : 'Effort', plan.effort, 'effort'), ...notes(plan.effort)));
      const perm = tkPlain(tkFlags().perm) || {};
      if (plan.approvals) {
        p.root.append(sec('APPROVALS', seg(p, 'Approvals', plan.approvals, 'approvals'),
          el('p', { class: 'tk-note', text: perm.bypass ? 'This session runs without approvals and without the sandbox. Start a new session to change that.'
            : perm.reviewer ? 'Approve for me is on: an automatic reviewer answers approval requests.' : 'on-request: Codex asks when it needs to. never: it does not ask, a failed command goes straight back to it.' }),
          ...notes(plan.approvals)));
      }
      if (plan.sandbox) {
        p.root.append(sec('SANDBOX', seg(p, 'Sandbox', plan.sandbox, 'sandbox'),
          el('p', { class: 'tk-note', text: 'read-only: nothing is written. workspace-write: the repo and the folders added to it. Skipping the sandbox is a launcher choice, not offered here.' }),
          ...notes(plan.sandbox)));
      }
      const auto = autoShown();
      if (plan.fast || plan.ultra || auto) {
        const row = plan.fast || plan.ultra ? el('div', { class: 'tk-toggles' }) : null;
        if (plan.fast) row.append(toggle(p, 'fast', 'Fast', 'Fast mode: quicker answers, a higher price (/fast on or /fast off)'));
        if (plan.ultra) row.append(toggle(p, 'ultra', 'Ultracode', 'Ultracode sets xhigh and turns workflows on, for this session only; it has no effect under -p'));
        const autoRow = auto ? el('div', { class: 'tk-toggles tk-solo' }, autoToggle(p)) : null;
        p.root.append(sec('OPTIONS', row, plan.ultra ? el('p', { class: 'tk-note', text: 'Ultracode sets xhigh and turns workflows on' }) : null, autoRow, auto ? p.auto.note : null));
      }
      if (plan.cells.length) {
        p.root.append(sec('COMMANDS', el('div', { class: 'tk-grid' }, ...plan.cells.map((s) => cell(p, s)))));
        if (plan.cells.some((s) => s.key === 'rename')) p.root.append(renameForm(p));
      }
      if (!plan.model && !plan.effort && !plan.fast && !plan.cells.length) p.root.append(el('p', { class: 'tk-gate', role: 'note', text: 'There is nothing to tune on this session.' }));
      p.root.append(p.readout);
    }

    function sync(p) {
      const plan = planNow();
      const gate = gateNow();
      const sig = tkAgent(c) + JSON.stringify([plan.model && [plan.model.via, plan.model.options.map((o) => o.value)], plan.effort && [plan.effort.cmd, plan.effort.via, plan.effort.options.map((o) => o.value)],
        plan.approvals && plan.approvals.options.map((o) => o.value), plan.sandbox && plan.sandbox.options.map((o) => o.value), plan.fast, plan.ultra, plan.cells.map((s) => s.key), autoShown(),
        (() => { const pm = tkPlain(tkFlags().perm) || {}; return [!!pm.bypass, !!pm.reviewer]; })()]);
      if (sig !== p.sig) { p.sig = sig; fill(p, plan); }
      const cur = currentNow(plan);
      if (T.pend && T.pend.satisfied(cur)) clearPending(true);
      const off = !gate.enabled || T.busy;
      p.gate.classList.toggle('hidden', gate.enabled || !gate.show);
      setText(p.gate, gate.title ? gate.title.charAt(0).toUpperCase() + gate.title.slice(1) + '.' : '');
      for (const n of p.nodes) {
        const b = n.node;
        b.disabled = off;
        if (!gate.enabled && gate.show) b.setAttribute('title', gate.title);
        let on = false;
        let known = true;
        if (n.kind === 'model') on = cur.model === n.value;
        else if (n.kind === 'effort') on = cur.effort === n.value.toLowerCase();
        else if (n.kind === 'approvals') on = cur.approval === n.value;
        else if (n.kind === 'sandbox') on = cur.sandbox === n.value;
        else if (n.kind === 'fast') { on = cur.fast === true; known = cur.fast !== null; }
        else if (n.kind === 'ultra') { on = cur.ultra === TK_ULTRA_ON; known = !!cur.ultra; }
        if (n.kind !== 'cell') { b.setAttribute('aria-pressed', on ? 'true' : 'false'); b.classList.toggle('on', on); }
        if (n.state) { setText(n.state, known ? (on ? 'on' : 'off') : 'unknown'); b.classList.toggle('tk-unknown', !known); }
        const key = n.kind === 'cell' ? '' : (n.kind === 'model' || n.kind === 'effort' ? n.kind + ':' + n.value : n.kind);
        b.classList.toggle('pending', !!T.pend && T.pend.key === key);
        b.classList.toggle('ok', !!T.ok && T.ok === key);
      }
      if (T.auto !== null && tkNoAuto(c) === T.auto) T.auto = null;         // the row the page passed in agrees now
      if (p.auto) {
        const off = autoOff();
        p.auto.node.setAttribute('aria-pressed', off ? 'false' : 'true');
        p.auto.node.classList.toggle('on', !off);
        setText(p.auto.state, off ? 'off' : 'on');
        setText(p.auto.note, boardAutoContinueOff() ? TK_AUTO_BOARD_OFF : TK_AUTO_WHAT);
      }
      if (p.rename) {
        p.rename.form.classList.toggle('hidden', !T.renaming);
        for (const b of p.rename.form.querySelectorAll('button')) b.disabled = T.busy;
      }
      p.readout.classList.toggle('hidden', !T.readout);
      if (T.readout && p.readout.getAttribute('data-cmd') !== T.readout.cmd + '|' + T.readout.text.length) {
        p.readout.setAttribute('data-cmd', T.readout.cmd + '|' + T.readout.text.length);
        p.readout.textContent = '';
        p.readout.append(el('div', { class: 'tk-gl', text: T.readout.cmd.toUpperCase() }), el('pre', { class: 'tk-pre', text: T.readout.text }),
          el('div', { class: 'tk-actions' }, el('button', { type: 'button', class: 'tk-send tk-close', text: 'Close', onclick: () => closeReadout() })));
      }
    }
    function syncAll() { for (const p of panels) sync(p); }

    function openRename() {
      T.renaming = true;
      syncAll();
      for (const p of panels) if (p.rename) {
        if (!p.rename.input.value) p.rename.input.value = String(tkStats(c).session_name || '');
        try { p.rename.input.focus(); if (typeof p.rename.input.select === 'function') p.rename.input.select(); } catch (_) { /* no focus */ }
      }
    }

    function openPanel(anchorEl, byKeyboard) {
      const p = newPanel(c.touch ? 'sheet' : 'pop');
      panels.add(p);
      sync(p);
      T.anchor = anchorEl || T.anchor;
      S.open(anchorEl, () => p.root, { touch: !!c.touch, title: 'Tune · ' + tkLabel(c), cls: 'tk-pop-tune', width: 320 });
      if (byKeyboard) { const first = p.root.querySelector('button:not([disabled])'); if (first) first.focus(); }
      else if (S.root && typeof S.root.focus === 'function') S.root.focus();
      return p;
    }

    function open(anchorEl, byKeyboard) {
      if (S.isOpen) { S.close(false); return; }
      openPanel(anchorEl, !!byKeyboard);
    }

    function mount(target) {
      if (!target || typeof target.append !== 'function') return null;
      const p = newPanel('mount');
      panels.add(p);
      sync(p);
      target.append(p.root);
      return {
        root: p.root,
        update(patch) { Object.assign(c, patch || {}); syncAll(); },
        destroy() { panels.delete(p); p.root.remove(); },
      };
    }

    function update(patch) {
      Object.assign(c, patch || {});
      syncAll();
    }

    function rename(anchorEl) {
      if (!S.isOpen) openPanel(anchorEl, false);
      openRename();
    }

    /* the tile is going: no pending-setting timer may toast about it later, the panels leave the page, and the agent's own dialog (a read command's output) is
       dismissed with the one Escape it needs */
    function destroy() {
      T.dead = true;
      clearTimeout(T.pendTimer);
      T.pendTimer = 0;
      T.pend = null;
      T.ok = null;
      T.renaming = false;
      const wasOpen = T.readout !== null || T.dialog;
      T.readout = null;
      S.close(false);
      for (const p of Array.from(panels)) p.root.remove();
      panels.clear();
      if (wasOpen) settleEscape();
    }

    return { open, close: (refocus) => S.close(!!refocus), mount, run, rename, update, destroy, get isOpen() { return S.isOpen; }, get root() { return S.root || S.body; } };
  }

  return {
    ttyUrl, bind, touchScroller, fitSoon, fitNow, viewportFit, keyBar, pressable, repeater, compactKeys, backTarget, contextParts, clampFont, FONT_MIN, FONT_MAX,
    termPane, paneLabel, paneParts, paneLine, panePending, sizeChip, typingTarget, ctxInfo, fitName,
    /* v0.5.9c quad v3: the tile menu, the prompt composer and the tune panel (termkit.css), with the pure helpers they and the tests share */
    tileMenu: makeTileMenu, composer: makeComposer, tune: makeTune, tuneGate, tunePlan, tuneCurrent, tuneRegistry, tuneRequest, tuneRestart,
    /* the opt-in font's outcome ('off' until a bind asked for it) and a promise of the final state; it never rejects */
    get fontState() { return fontState; },
    get fontReady() { return fontPromise || Promise.resolve(fontState); },
  };
})();
