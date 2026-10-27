Pages.peekTmux = function () {
  const r = typeof currentRoute === 'function' ? currentRoute() : null;
  return r && r.id === 'session' && r.params ? r.params.tmux : null;
};

/* The session a key or a palette nudge acts on: the selected row, unless a peek was opened after that selection (then the peek), else the peek. */
Pages.target = function () {
  const sel = Pages.selected();
  const peek = Pages.peekTmux();
  if (sel && (!peek || Pages.selHash === Pages.hashNow())) return sel.tmux;
  return peek || null;
};

Pages.session = function (tmux) { return tmux ? (Pages.order().find((x) => x.tmux === tmux) || Inbox.items(state).find((x) => x.tmux === tmux) || null) : null; };

Pages.targetPerm = function () {
  const t = Pages.target();
  return t && typeof sessionPerm === 'function' ? sessionPerm(t, true) : null;
};

/* mod+1..9: open the nth session of Pages.order() as the peek. False (key left alone) when there is no nth session. */
Pages.openNth = function (n) {
  const s = Pages.order()[n - 1];
  if (!s) return false;
  if (s.bare_device) { bareTerminal(s); return true; }
  navigate(sessionHash(s.tmux));
  return true;
};

/* The peek's send box (session.js builds it, a composer textarea): the node, or null while no peek is open. */
Pages.sendBox = function () {
  if (typeof sessionPeek !== 'undefined' && sessionPeek.parts && sessionPeek.parts.input && sessionPeek.parts.input.isConnected !== false) return sessionPeek.parts.input;
  try { return document.querySelector('.peek-send textarea, .peek-send input'); } catch (_) { return null; }
};

/* Run fn(box) as soon as the peek's send box exists: now, or within about half a second (a peek opened by the same key press mounts on hashchange). */
Pages.withSendBox = function (fn, tries) {
  const box = Pages.sendBox();
  if (box) { fn(box); return true; }
  const left = typeof tries === 'number' ? tries : 8;
  if (left <= 0 || typeof setTimeout !== 'function') return false;
  Pages.retry = setTimeout(() => Pages.withSendBox(fn, left - 1), 60);
  return true;
};

/* what: peek | term | ack | allow | deny | reply. Returns true when something was done (or started). */
Pages.act = async function (what) {
  if (what === 'peek') {
    const s = Pages.selected();
    if (!s) return false;
    if (s.bare_device) { await bareTerminal(s); return true; }
    const h = sessionHash(s.tmux);
    Pages.selHash = h;
    navigate(h);
    return true;
  }
  const tmux = Pages.target();
  if (!tmux) return false;
  const s = Pages.session(tmux);
  const name = (s && s.name) || tmux;
  if (what === 'term') {                                       // the same surface as a click on the row's Open link: the dock from 1024 px up, else the terminal page
    if (s && s.bare_device) { await bareTerminal(s); return true; }
    if (typeof Shell !== 'undefined' && Shell && typeof Shell.openTerm === 'function') Shell.openTerm(tmux);
    else openPage(`/term/${encodeURIComponent(tmux)}`);
    return true;
  }
  if (what === 'reply') {
    if (s && s.bare_device) {
      const card = Array.from(document.querySelectorAll('.inbox-card')).find((n) => n.getAttribute('data-tmux') === tmux);
      if (!card) return false;
      card.classList.add('open');
      const box = card.querySelector('textarea');
      if (box) box.focus();
      return !!box;
    }
    if (!Pages.peekTmux() || Pages.peekTmux() !== tmux) { const h = sessionHash(tmux); Pages.selHash = h; navigate(h); }
    return Pages.withSendBox((box) => box.focus());
  }
  if (what === 'ack') {
    if (s && !s.needs_attention) { pageToast(`${name} has nothing to acknowledge`, 'info'); return false; }
    if (!(await sessionAck(s || { tmux }))) return false;
    pageToast(`acknowledged ${name}`, 'ok');
    return true;
  }
  if (what === 'allow' || what === 'deny') {
    const pr = typeof sessionPerm === 'function' ? sessionPerm(tmux, true) : null;
    if (!pr) { pageToast(`no permission is waiting on ${name}`, 'warn'); return false; }
    if (!(await decide(pr.id, what))) return false;
    pageToast(`${what === 'allow' ? 'allowed' : 'denied'} ${name}`, what === 'allow' ? 'ok' : 'warn');
    return true;
  }
  return false;
};

