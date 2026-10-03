/* ccboard termkit (v0.5.8, terminal page v2): everything the terminal page needs around a ttyd iframe, as one namespace.
   Definition only, like core.js and components.js: loading this file touches no DOM, storage, network, listener or timer; the
   page script (term.js) calls into it. Classic script (no modules). Loaded by term.html between components.js and term.js.

     TermKit.ttyUrl(name, {mode, fontSize, renderer, quiet})   the iframe URL for ttyd (mode whitelisted: full | grid | ro)
     TermKit.bind(iframe, {onActive, touchScroll, fontSize})   per iframe load: poll window.term, activity events, font size,
                                                               touch-to-wheel shim, overscroll hardening -> handle
     TermKit.touchScroller({step, threshold, edge, momentum, onWheel})   the shim's accumulator, testable on its own
     TermKit.fitSoon(iframe)                                   debounced term.fit() (resize event when fit() is missing)
     TermKit.viewportFit()                                     --vvh / --vvt from visualViewport (soft keyboard aware)
     TermKit.keyBar(host, {send, sendText, scroll, compact})   the 44 px key bar -> {root, setCompact(on), setApproval(on)}
     TermKit.pressable(button, fire, opts) / TermKit.repeater(fire, opts)   press and hold-to-repeat, soft keyboard stays up
     TermKit.contextParts(session) / compactKeys(...) / backTarget(...) / clampFont(...)   small pure helpers */
'use strict';

const TermKit = (() => {
  const MODES = ['full', 'grid', 'ro'];
  const RENDERERS = ['canvas', 'dom', 'webgl'];
  const FONT_MIN = 8;
  const FONT_MAX = 28;
  const GRID_FONT = 11;
  const BIND_TIMEOUT_MS = 10000;
  const BIND_POLL_MS = 100;

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
    handle.destroy = () => { generation += 1; stopPoll(); iframe.removeEventListener('load', onLoad); };

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
        } catch (_) { return true; }
        if (Date.now() - started > BIND_TIMEOUT_MS) { if (w) harden(w.document); return true; }
        return false;
      };
      if (step()) return;
      pollTimer = setInterval(() => { if (step()) stopPoll(); }, BIND_POLL_MS);
    }
    iframe.addEventListener('load', onLoad);
    return handle;
  }

  /* ---- the key bar ------------------------------------------------------------------------------------------------- */

  /* label: visible text; short: label in compact mode; key: tmux key name for send(); scroll: argument of scroll(); title: tooltip and
     aria-label; repeat: hold-to-repeat. The scroll keys never send raw keys: the server decides between copy-mode and PageUp. */
  const KEY_ROWS = [
    [
      { label: 'Esc', key: 'Escape', title: 'Escape' },
      { label: 'Tab', key: 'Tab', title: 'Tab' },
      { label: 'Shift+Tab', short: '⇧Tab', key: 'BTab', title: 'Shift+Tab' },
      { label: 'Ctrl-C', short: '^C', key: 'C-c', title: 'Ctrl-C' },
      { label: 'Enter', key: 'Enter', title: 'Enter' },
    ],
    [
      { label: '↑', key: 'Up', title: 'Up', repeat: true },
      { label: '↓', key: 'Down', title: 'Down', repeat: true },
      { label: '←', key: 'Left', title: 'Left', repeat: true },
      { label: '→', key: 'Right', title: 'Right', repeat: true },
      { label: '⌫', key: 'BSpace', title: 'Backspace', repeat: true },
    ],
    [
      { label: 'PgUp', scroll: 'up', title: 'Page up (scroll back)', repeat: true },
      { label: 'PgDn', scroll: 'down', title: 'Page down (scroll forward)', repeat: true },
      { label: 'Top', scroll: 'top', title: 'Scroll to the top' },
      { label: 'Bottom', scroll: 'bottom', title: 'Scroll to the bottom' },
      { label: 'Ctrl+O', key: 'C-o', title: 'Ctrl+O' },
    ],
  ];

  /* keyBar(host, {send(keys[]), sendText(text, enter), scroll(dir), compact}) -> {root, setCompact(on), setApproval(on)}.
     Rows: Esc Tab Shift+Tab Ctrl-C Enter / arrows and backspace / PgUp PgDn Top Bottom Ctrl+O. Every key is at least 44 px
     (term.css). Holding an arrow or backspace repeats it after 400 ms, every 90 ms, batched into one send() of at most 20 keys.
     Compact (soft keyboard up) keeps Esc ^C Tab Shift+Tab Enter and a More toggle for the other rows. setApproval(true) adds a
     y / n row (a pending permission: remote approve is off while this page is open, the answer is typed into the terminal). */
  function keyBar(host, opts) {
    const o = opts || {};
    const send = typeof o.send === 'function' ? o.send : () => {};
    const sendText = typeof o.sendText === 'function' ? o.sendText : () => {};
    const scroll = typeof o.scroll === 'function' ? o.scroll : () => {};
    const shorts = [];

    function keyButton(spec) {
      const b = el('button', { type: 'button', class: 'kb-key', 'aria-label': spec.title || spec.label, title: spec.title || spec.label, 'data-key': spec.key || spec.scroll, text: spec.label });
      if (spec.short) shorts.push({ node: b, long: spec.label, short: spec.short });
      if (spec.scroll) pressable(b, () => scroll(spec.scroll), spec.repeat ? { repeat: true, every: 150, backlog: false } : {});
      else pressable(b, (n) => send(new Array(n).fill(spec.key)), spec.repeat ? { repeat: true, delay: 400, every: 90, max: 20 } : {});
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

  return { ttyUrl, bind, touchScroller, fitSoon, fitNow, viewportFit, keyBar, pressable, repeater, compactKeys, backTarget, contextParts, clampFont, FONT_MIN, FONT_MAX };
})();
