/* ccboard termkit (v0.5.8, terminal page v2): everything the terminal page needs around a ttyd iframe, as one namespace.
   Definition only, like core.js and components.js: loading this file touches no DOM, storage, network, listener or timer; the
   page script (term.js) calls into it. Classic script (no modules). Loaded by term.html between components.js and term.js.

     TermKit.ttyUrl(name, {mode, fontSize, renderer, quiet})   the iframe URL for ttyd (mode whitelisted: full | grid | ro)
     TermKit.bind(iframe, {onActive, touchScroll, fontSize, font})   per iframe load: poll window.term, activity events, font size,
                                                               touch-to-wheel shim, overscroll hardening, and (flag only) the
                                                               JetBrains Mono spike -> handle
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
                                                               sizeChip / typingTarget / ctxInfo (the context chip's one reading, dock and quad) are exported */
'use strict';

const TermKit = (() => {
  const MODES = ['full', 'grid', 'ro'];
  const RENDERERS = ['canvas', 'dom', 'webgl'];
  const FONT_MIN = 8;
  const FONT_MAX = 28;
  const GRID_FONT = 11;
  const BIND_TIMEOUT_MS = 10000;
  const BIND_POLL_MS = 100;

  /* The font spike (v0.5.8, OFF by default): ttyd's own page asks for system fonts, which differ per device (a phone has no Menlo, no
     Consolas), so cell width, cursor and box drawing vary. bind() can load the vendored JetBrains Mono into the iframe instead. Verdict
     on real devices first: ccboard:term:font=1 (or bind's `font: true`) turns it on; see TermKit.fontState. */
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

  /* ---- font spike state ------------------------------------------------------------------------------------------- */

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

  /* The spike itself, for one ttyd window (same origin: the caller already read w.document). Order matters: the face is loaded and added
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
       - the font spike, only when `font` is true (an explicit false wins) or, with `font` left out, ccboard:term:font = '1' in
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
    let fontTry = null;                       // this binding's current font attempt (null: the spike is off for this load)
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

  return {
    ttyUrl, bind, touchScroller, fitSoon, fitNow, viewportFit, keyBar, pressable, repeater, compactKeys, backTarget, contextParts, clampFont, FONT_MIN, FONT_MAX,
    termPane, paneLabel, paneParts, paneLine, panePending, sizeChip, typingTarget, ctxInfo, fitName,
    /* the font spike's outcome ('off' until a bind asked for it) and a promise of the final state; it never rejects */
    get fontState() { return fontState; },
    get fontReady() { return fontPromise || Promise.resolve(fontState); },
  };
})();
