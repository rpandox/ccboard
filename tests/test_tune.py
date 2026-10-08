"""POST /api/sessions/{name}/tune and app/agents/pickers.py (v0.5.21, issues #2 and #78): session-only tuning through the agent's own pickers.

lite_client + fake_tmux, with a small fake TUI on top: it watches the keys and text the board sends (the fake tmux records them) and draws
what Claude Code 2.1.290 and codex 0.160.1 drew in the box checks V8 / V8-Codex (issues #16, #28, #77): Codex's numbered pickers with the
cursor on the current entry, `s` = this session only, Enter = save the default, one Escape = one level back; Claude's /effort slider with
the marker on the current level. No real claude or codex runs; nothing touches the real home.
"""
import pytest

from app import main, tmux
from app.agents import codex, pickers
from app.config import settings

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
NAME = "shop--api--s1"
CODEX_MODELS = ["GPT-6.1-Sol", "GPT-6-Astra", "GPT-6-Sol", "GPT-6-Luna", "GPT-5.6-Sol", "GPT-5.6-Terra", "GPT-5.6-Luna"]
LEVELS = {"gpt-6.1-sol": ["low", "medium", "high", "xhigh"], "gpt-6-luna": ["low", "medium", "high", "xhigh"]}
LABEL = {"low": "Low", "medium": "Medium", "high": "High", "xhigh": "Extra high", "max": "Max", "ultra": "Ultra"}
PERMS = ["Ask for approval", "Approve for me", "Full Access"]


class FakeTUI:
    """Enough of a TUI to drive: a history, an open picker (or none) and its cursor. `marks`: draw the `›` cursor mark (False: a TUI
    whose cursor is a colour only, which a plain capture cannot see)."""

    def __init__(self, agent, model="gpt-6.1-sol", effort="low", perms=0, marks=True):
        self.agent, self.model, self.effort, self.perms, self.marks = agent, model, effort, perms, marks
        self.view, self.cur, self.pick_model = None, 0, None
        self.history: list[str] = []
        self.saved_default = False
        self.draft = ""
        self.log: list = []

    # -- input --
    def keys(self, keys):
        for k in keys:
            self.log.append(k)
            self.key(k)

    def text(self, text, enter):
        self.log.append(("text", text, enter))
        if self.view and not enter and len(text) == 1:
            return self.key(text)
        self.draft += text
        if enter:
            line, self.draft = self.draft, ""
            self.submit(line)

    def key(self, k):
        if k == "C-u":
            if self.view is None:
                self.draft = ""
            return
        v = self.view
        if v is None:
            return
        n = len(self.items())
        if k in ("Down", "Right"):
            self.cur = (self.cur + 1) % n if self.agent == "codex" else min(self.cur + 1, n - 1)
        elif k in ("Up", "Left"):
            self.cur = (self.cur - 1) % n if self.agent == "codex" else max(self.cur - 1, 0)
        elif k == "Escape":
            self.view = {"level": "model", "advanced": "level"}.get(v)
            if self.view == "model":
                self.cur = self.idx(self.pick_model)
        elif k == "Enter":
            self.enter(save=True)
        elif k == "s" and v in ("level", "advanced", "slider"):
            self.enter(save=False)

    def idx(self, slug):
        return [m.lower() for m in CODEX_MODELS].index(slug)

    def items(self):
        if self.view == "model":
            return CODEX_MODELS
        if self.view == "level":
            return [LABEL[x] for x in LEVELS.get(self.pick_model, ["low", "medium", "high", "xhigh"])] + ["More reasoning…"]
        if self.view == "advanced":
            return ["Max", "Ultra"] if self.pick_model == "gpt-6.1-sol" else ["Max"]
        if self.view == "perms":
            return PERMS
        if self.view == "slider":
            return ["low", "medium", "high", "xhigh", "max"]
        return []

    def submit(self, line):
        self.history.append("› " + line)
        if self.agent == "codex":
            if line == "/model":
                self.view, self.cur = "model", self.idx(self.model)
            elif line == "/permissions":
                self.view, self.cur = "perms", self.perms
            elif line == "/status":
                self.history.append(f"  Model:        {self.model} (reasoning {self.effort})")
                self.history.append(f"  Permissions:  Workspace ({PERMS[self.perms]})")
            elif line.startswith("/model "):
                self.history.append("(sent to the model as a prompt)")
        else:
            if line == "/effort":
                self.view, self.cur = "slider", ["low", "medium", "high", "xhigh", "max"].index(self.effort)
            elif line == "/effort ultracode on":
                self.history.append("Ultracode on (this session only): dynamic workflows on every task. Effort stays high.")
            elif line == "/effort ultracode off":
                self.history.append("Ultracode off. Effort stays high.")

    def enter(self, save):
        v = self.view
        if v == "model":
            self.pick_model = CODEX_MODELS[self.cur].lower()
            levels = LEVELS.get(self.pick_model, ["low", "medium", "high", "xhigh"])
            start = self.effort if self.pick_model == self.model else "medium"
            self.view, self.cur = "level", levels.index(start) if start in levels else 0
        elif v == "level":
            label = self.items()[self.cur]
            if label.startswith("More"):
                self.view, self.cur = "advanced", 0
                return
            self.apply([k for k, x in LABEL.items() if x == label][0], save)
        elif v == "advanced":
            self.apply(self.items()[self.cur].lower(), save)
        elif v == "perms":
            self.perms = self.cur
            self.history.append(f"Permission selection requested: {PERMS[self.cur]}")
            self.view = None
        elif v == "slider":
            self.effort = ["low", "medium", "high", "xhigh", "max"][self.cur]
            self.saved_default = save
            self.view = None

    def apply(self, level, save):
        self.model, self.effort, self.saved_default, self.view = self.pick_model, level, save, None
        self.history.append(f"Model changed to {self.model} {level}" + ("" if save else " for this session only"))

    # -- output --
    def screen(self):
        lines = list(self.history)
        if self.view in ("model", "level", "advanced", "perms"):
            lines.append({"model": "  Select Model and Effort", "level": f"  Select Reasoning Level for {self.pick_model}",
                          "advanced": "  Advanced Reasoning", "perms": "  Update Model Permissions"}[self.view])
            for i, label in enumerate(self.items()):
                mark = "›" if (i == self.cur and self.marks) else " "
                lines.append(f"{mark} {i + 1}. {label}" + ("   (current)" if self.view == "model" and label.lower() == self.model else ""))
            lines.append("  enter default · s session · esc back")
        elif self.view == "slider":
            lines.append("  Effort  " + "  ".join(("[" + x + "]") if i == self.cur else x for i, x in enumerate(self.items())))
            lines.append("  Enter to confirm · s for this session only · Esc to cancel")
        else:
            lines.append("› " + self.draft)
        return "\n".join(lines)


@pytest.fixture
def tui(monkeypatch, fake_tmux):
    holder = {}

    def keys(name, ks):
        holder["tui"].keys(list(ks))

    def text(name, t, enter=False):
        holder["tui"].text(t, enter)

    monkeypatch.setattr(tmux, "send_keys", keys)
    monkeypatch.setattr(tmux, "send_text", text)
    monkeypatch.setattr(tmux, "capture", lambda name, lines=200, join=True, escapes=False: holder["tui"].screen())
    monkeypatch.setattr(main, "_tune_sleep", lambda s: None)
    monkeypatch.setattr(settings, "codex_bin", lambda: None)            # the fallback catalogue, no subprocess
    codex.reset_caches()

    def make(agent, **kw):
        holder["tui"] = FakeTUI(agent, **kw)
        return holder["tui"]
    return make


def session(fake_tmux, agent, stats, state="idle"):
    main.db.add_session(tmux_name=NAME, project="shop", repo="api", name="s1", launcher="claude", agent=agent)
    fake_tmux["sessions"][NAME] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%1", "command": agent, "path": "/x", "pid": 1, "env": {}}
    main.db.set_state(NAME, state, "SessionStart")
    main.db.set_stats(NAME, stats)


def tune(client, setting, value):
    return client.post(f"/api/sessions/{NAME}/tune", headers=H, json={"setting": setting, "value": value})


def flags():
    return main.db.open_row(NAME)["flags"]


# ================================================================ Codex: the /model picker

def test_codex_model_moves_from_the_current_row_and_applies_for_this_session_only(lite_client, fake_tmux, tui):
    t = tui("codex", model="gpt-6.1-sol", effort="low")
    session(fake_tmux, "codex", {"model": "gpt-6.1-sol", "effort": "low"})
    r = tune(lite_client, "model", "gpt-6-luna")
    assert r.status_code == 200, r.text
    b = r.json()
    assert (b["confirmed"], b["verified"], b["session_only"], b["observed"]) == (True, True, True, "gpt-6-luna medium")
    assert t.model == "gpt-6-luna" and t.saved_default is False, "`s`, never Enter, on the reasoning step"
    # exactly: clear the draft, the bare command, three rows down, Enter, then `s`
    assert t.log == ["C-u", ("text", "/model", True), "Down", "Down", "Down", "Enter", ("text", "s", False)]
    assert not any(isinstance(x, tuple) and x[1].startswith("/model ") for x in t.log), "never `/model <slug>` (Codex sends it to the model)"
    f = flags()
    assert f["last_cmd"]["confirmed"] is True and f["last_cmd"]["cmd"] == "model" and "pending_cmd" not in f
    assert f["tuned"]["model"]["value"] == "gpt-6-luna"


def test_codex_model_up_from_below(lite_client, fake_tmux, tui):
    t = tui("codex", model="gpt-6-luna", effort="medium")
    session(fake_tmux, "codex", {"model": "gpt-6-luna", "effort": "medium"})
    assert tune(lite_client, "model", "gpt-6.1-sol").json()["confirmed"] is True
    assert t.log[2:5] == ["Up", "Up", "Up"] and t.model == "gpt-6.1-sol"


def test_codex_reasoning_stays_on_the_current_model_and_picks_the_level(lite_client, fake_tmux, tui):
    t = tui("codex", model="gpt-6.1-sol", effort="low")
    session(fake_tmux, "codex", {"model": "gpt-6.1-sol", "effort": "low"})
    b = tune(lite_client, "reasoning", "xhigh").json()
    assert b["confirmed"] is True and b["verified"] is False, "step 2's starting row was not seen on the box: unverified"
    assert t.log == ["C-u", ("text", "/model", True), "Enter", "Down", "Down", "Down", ("text", "s", False)]
    assert (t.model, t.effort, t.saved_default) == ("gpt-6.1-sol", "xhigh", False)


def test_codex_reasoning_max_and_ultra_go_through_more_reasoning(lite_client, fake_tmux, tui):
    t = tui("codex", model="gpt-6.1-sol", effort="high")
    session(fake_tmux, "codex", {"model": "gpt-6.1-sol", "effort": "high"})
    assert tune(lite_client, "reasoning", "ultra").json()["confirmed"] is True
    # step 1: Enter on the current model; step 2: High -> More reasoning (2 down), Enter; Advanced: Max -> Ultra, `s`
    assert t.log == ["C-u", ("text", "/model", True), "Enter", "Down", "Down", "Enter", "Down", ("text", "s", False)]
    assert (t.effort, t.saved_default) == ("ultra", False)


def test_codex_ultra_is_refused_for_a_model_without_it(lite_client, fake_tmux, tui):
    t = tui("codex", model="gpt-6-luna", effort="medium")
    session(fake_tmux, "codex", {"model": "gpt-6-luna", "effort": "medium"})
    r = tune(lite_client, "reasoning", "ultra")
    assert r.status_code == 409 and r.json()["error"] == "cannot_place" and "max" in r.json()["message"]
    assert t.log == [], "nothing typed"


def test_codex_reasoning_needs_the_current_model(lite_client, fake_tmux, tui):
    t = tui("codex")
    session(fake_tmux, "codex", {})
    r = tune(lite_client, "reasoning", "high")
    assert r.status_code == 409 and r.json()["error"] == "cannot_place" and t.log == []


def test_a_cursor_the_screen_does_not_show_is_assumed_only_where_the_box_proved_it(lite_client, fake_tmux, tui):
    """Step 1 (the cursor starts on the current model: seen on the box) may be driven without a visible mark; step 2 may not."""
    t = tui("codex", model="gpt-6.1-sol", effort="low", marks=False)
    session(fake_tmux, "codex", {"model": "gpt-6.1-sol", "effort": "low"})
    assert tune(lite_client, "model", "gpt-6-sol").json()["confirmed"] is True and t.model == "gpt-6-sol"
    # reasoning: the assumed row is the current level (`low`): allowed, and the read-back still decides
    t2 = tui("codex", model="gpt-6-sol", effort="medium", marks=False)
    main.db.set_stats(NAME, {"model": "gpt-6-sol", "effort": "medium"})
    b = tune(lite_client, "reasoning", "high").json()
    assert b["confirmed"] is True and t2.effort == "high"


def test_a_picker_that_does_not_open_is_backed_out_of_and_nothing_is_chosen(lite_client, fake_tmux, tui, monkeypatch):
    t = tui("codex", model="gpt-6.1-sol", effort="low")
    session(fake_tmux, "codex", {"model": "gpt-6.1-sol", "effort": "low"})
    monkeypatch.setattr(t, "submit", lambda line: t.history.append("› " + line))          # a TUI that shows no picker
    r = tune(lite_client, "model", "gpt-6-luna")
    assert r.status_code == 409 and r.json()["error"] == "picker"
    assert t.log[-1] == "Escape" and "Enter" not in t.log and ("text", "s", False) not in t.log
    assert "pending_cmd" not in flags()


def test_an_older_line_on_the_screen_never_passes_for_an_open_picker(lite_client, fake_tmux, tui, monkeypatch):
    """Only what the TUI drew after the command counts: an earlier "Ultracode on (this session only)" line, or a model answer that says
    "Select Model and Effort", must not make the board press keys into the composer."""
    t = tui("claude", effort="high")
    session(fake_tmux, "claude", {"model": "Opus 5", "effort": "high"})
    t.history += ["Ultracode on (this session only): dynamic workflows on every task. Effort stays high.",
                  "  Enter to confirm · s for this session only · Esc to cancel"]
    monkeypatch.setattr(t, "submit", lambda line: t.history.append("› " + line))          # /effort opens nothing this time
    r = tune(lite_client, "effort", "max")
    assert r.status_code == 409 and r.json()["error"] == "picker"
    assert "Right" not in t.log and ("text", "s", False) not in t.log and t.log[-1] == "Escape"


def test_an_older_codex_picker_on_the_screen_is_not_the_one_just_opened(lite_client, fake_tmux, tui, monkeypatch):
    t = tui("codex", model="gpt-6.1-sol", effort="low")
    session(fake_tmux, "codex", {"model": "gpt-6.1-sol", "effort": "low"})
    t.history += ["  Select Model and Effort", "› 1. GPT-6.1-Sol", "  2. GPT-6-Astra", "  3. GPT-6-Sol", "  4. GPT-6-Luna"]
    monkeypatch.setattr(t, "submit", lambda line: t.history.append("› " + line))
    r = tune(lite_client, "model", "gpt-6-luna")
    assert r.status_code == 409 and r.json()["error"] == "picker" and "Down" not in t.log and "Enter" not in t.log


def test_a_catalogue_display_name_matches_the_picker_row(lite_client, fake_tmux, tui, monkeypatch):
    """The live catalogue's display name ("GPT-6.1 Sol") and the row the picker draws ("GPT-6.1-Sol") are one entry."""
    t = tui("codex", model="gpt-6.1-sol", effort="low")
    session(fake_tmux, "codex", {"model": "gpt-6.1-sol", "effort": "low"})
    named = [{"slug": "gpt-6.1-sol", "name": "GPT-6.1 Sol", "reasoning": ["low", "medium", "high", "xhigh", "max", "ultra"]},
             {"slug": "gpt-6-astra", "name": "GPT-6 Astra", "reasoning": ["low", "medium", "high", "xhigh", "max"]},
             {"slug": "gpt-6-sol", "name": "GPT-6 Sol", "reasoning": ["low", "medium", "high", "xhigh", "max"]},
             {"slug": "gpt-6-luna", "name": "GPT-6 Luna", "reasoning": ["low", "medium", "high", "xhigh", "max"]}]
    monkeypatch.setattr(codex.CodexAgent, "models", lambda self, fetch=True: [dict(m) for m in named])
    assert tune(lite_client, "model", "gpt-6-sol").json()["confirmed"] is True and t.model == "gpt-6-sol"
    assert t.log[2:4] == ["Down", "Down"]


def test_a_cursor_that_does_not_land_backs_out(lite_client, fake_tmux, tui, monkeypatch):
    t = tui("codex", model="gpt-6.1-sol", effort="low")
    session(fake_tmux, "codex", {"model": "gpt-6.1-sol", "effort": "low"})
    real = t.key
    monkeypatch.setattr(t, "key", lambda k: None if k == "Down" else real(k))             # Down is swallowed
    r = tune(lite_client, "model", "gpt-6-luna")
    assert r.status_code == 409 and r.json()["error"] == "picker" and "did not land" in r.json()["message"]
    assert t.model == "gpt-6.1-sol" and t.log[-1] == "Escape" and "Enter" not in t.log


def test_a_readback_that_shows_another_value_is_not_confirmed(lite_client, fake_tmux, tui, monkeypatch):
    t = tui("codex", model="gpt-6.1-sol", effort="low")
    session(fake_tmux, "codex", {"model": "gpt-6.1-sol", "effort": "low"})
    monkeypatch.setattr(t, "apply", lambda level, save: (t.history.append("Model changed to gpt-6-sol low for this session only"),
                                                         setattr(t, "view", None)))
    b = tune(lite_client, "model", "gpt-6-luna").json()
    assert b["confirmed"] is False and "gpt-6-sol" in b["message"] and flags()["last_cmd"]["confirmed"] is False


def test_no_readback_leaves_the_rollout_to_confirm(lite_client, fake_tmux, tui, monkeypatch):
    t = tui("codex", model="gpt-6.1-sol", effort="low")
    session(fake_tmux, "codex", {"model": "gpt-6.1-sol", "effort": "low"})
    monkeypatch.setattr(t, "apply", lambda level, save: setattr(t, "view", None))           # nothing printed
    b = tune(lite_client, "model", "gpt-6-luna").json()
    assert b["confirmed"] is None and "waiting" in b["message"]
    pend = flags()["pending_cmd"]
    assert (pend["cmd"], pend["arg"], pend["before"]["model"]) == ("model", "gpt-6-luna", "gpt-6.1-sol")


# ================================================================ Codex: /permissions

def test_codex_permissions_reads_the_cursor_then_confirms_with_status(lite_client, fake_tmux, tui):
    t = tui("codex", perms=0)
    session(fake_tmux, "codex", {"model": "gpt-6.1-sol", "effort": "low"})
    b = tune(lite_client, "permissions", "auto").json()
    assert (b["confirmed"], b["observed"], b["verified"]) == (True, "Approve for me", False)
    assert t.log == ["C-u", ("text", "/permissions", True), "Down", "Enter", "C-u", ("text", "/status", True)]
    assert t.perms == 1 and flags()["tuned"]["permissions"]["value"] == "auto"
    b = tune(lite_client, "permissions", "ask").json()
    assert b["confirmed"] is True and t.perms == 0


def test_codex_permissions_never_guesses_the_cursor_next_to_full_access(lite_client, fake_tmux, tui):
    t = tui("codex", perms=1, marks=False)
    session(fake_tmux, "codex", {"model": "gpt-6.1-sol", "effort": "low"})
    r = tune(lite_client, "permissions", "ask")
    assert r.status_code == 409 and r.json()["error"] == "picker" and "cursor" in r.json()["message"]
    assert t.perms == 1 and "Enter" not in t.log and t.log[-1] == "Escape"


@pytest.mark.parametrize("value", ["full", "full access", "danger-full-access", "untrusted", "on-failure", "never"])
def test_full_access_and_raw_policies_are_not_tune_values(lite_client, fake_tmux, tui, value):
    t = tui("codex")
    session(fake_tmux, "codex", {"model": "gpt-6.1-sol"})
    r = tune(lite_client, "permissions", value)
    assert r.status_code == 409 and r.json()["error"] == "cannot_place" and t.log == []


# ================================================================ Claude: /effort (picker, session only) and ultracode

@pytest.mark.parametrize("cur,want,keys", [("medium", "xhigh", ["Right", "Right"]), ("max", "low", ["Left"] * 4), ("high", "high", [])])
def test_claude_effort_goes_through_the_picker_and_s(lite_client, fake_tmux, tui, cur, want, keys):
    t = tui("claude", effort=cur)
    session(fake_tmux, "claude", {"model": "Opus 5", "effort": cur})
    b = tune(lite_client, "effort", want).json()
    assert b["confirmed"] is None and b["session_only"] is True and b["verified"] is False, "`s` on the slider was not pressed on the box"
    assert t.log == ["C-u", ("text", "/effort", True), *keys, ("text", "s", False)]
    assert t.effort == want and t.saved_default is False, "`s`: this session only, the default untouched"
    pend = flags()["pending_cmd"]
    assert (pend["cmd"], pend["arg"], pend["before"]["effort"]) == ("effort", want, cur)   # the statusline confirms it


def test_claude_effort_needs_the_current_level(lite_client, fake_tmux, tui):
    t = tui("claude")
    session(fake_tmux, "claude", {"model": "Opus 5"})
    r = tune(lite_client, "effort", "high")
    assert r.status_code == 409 and r.json()["error"] == "cannot_place" and t.log == []
    assert tune(lite_client, "effort", "ultracode").status_code == 409, "ultracode is not a level"


@pytest.mark.parametrize("value,line", [("on", "/effort ultracode on"), ("off", "/effort ultracode off")])
def test_ultracode_sends_the_documented_commands_and_reads_the_pane(lite_client, fake_tmux, tui, value, line):
    t = tui("claude", effort="high")
    session(fake_tmux, "claude", {"model": "Opus 5", "effort": "high"})
    b = tune(lite_client, "ultracode", value).json()
    assert t.log == ["C-u", ("text", line, True)]
    assert (b["confirmed"], b["observed"]) == (True, value)
    f = flags()
    assert f["tuned"]["ultracode"]["value"] == value and f["last_cmd"]["cmd"] == "ultracode" and "pending_cmd" not in f


def test_ultracode_not_shown_on_the_pane_is_not_confirmed(lite_client, fake_tmux, tui, monkeypatch):
    t = tui("claude", effort="high")
    session(fake_tmux, "claude", {"model": "Opus 5", "effort": "high"})
    monkeypatch.setattr(t, "submit", lambda line: t.history.append("› " + line))
    b = tune(lite_client, "ultracode", "off").json()
    assert b["confirmed"] is False and "not confirmed" in b["message"] and "pending_cmd" not in flags(), "no statusline field could settle it"


# ================================================================ guards

@pytest.mark.parametrize("agent,setting", [("claude", "model"), ("claude", "fast"), ("codex", "effort"), ("codex", "fast"), ("codex", "sandbox"),
                                           ("codex", "approvals"), ("claude", ""), ("codex", None)])
def test_unknown_settings_are_400(lite_client, fake_tmux, tui, agent, setting):
    t = tui(agent)
    session(fake_tmux, agent, {"model": "gpt-6.1-sol", "effort": "low"})
    r = lite_client.post(f"/api/sessions/{NAME}/tune", headers=H, json={"setting": setting, "value": "x"})
    assert r.status_code == 400 and t.log == []


def test_a_busy_session_is_409_and_nothing_is_typed(lite_client, fake_tmux, tui):
    t = tui("codex")
    session(fake_tmux, "codex", {"model": "gpt-6.1-sol", "effort": "low"}, state="working")
    r = tune(lite_client, "model", "gpt-6-luna")
    assert r.status_code == 409 and r.json()["error"] == "working" and t.log == []


def test_one_drive_per_session_at_a_time(lite_client, fake_tmux, tui):
    t = tui("codex")
    session(fake_tmux, "codex", {"model": "gpt-6.1-sol", "effort": "low"})
    lock = pickers.session_lock(NAME)
    assert lock.acquire(blocking=False)
    try:
        r = tune(lite_client, "model", "gpt-6-luna")
        assert r.status_code == 409 and r.json()["error"] == "busy" and t.log == []
    finally:
        lock.release()


def test_command_refuses_a_picker_driven_spec(lite_client, fake_tmux, tui):
    """Codex's /model never goes through /command (it would type `/model <slug>`, which Codex sends to the model as a prompt)."""
    t = tui("codex")
    session(fake_tmux, "codex", {"model": "gpt-6.1-sol"})
    for body in ({"cmd": "model", "arg": "gpt-6-luna"}, {"cmd": "model"}, {"cmd": "reasoning", "arg": "high"}, {"cmd": "permissions"}):
        r = lite_client.post(f"/api/sessions/{NAME}/command", headers=H, json=body)
        assert r.status_code == 400 and "POST /api/sessions/" in r.json()["error"] and "/tune" in r.json()["error"], body
    assert t.log == []
    r = lite_client.post(f"/api/sessions/{NAME}/command", headers=H, json={"cmd": "status", "wait_ms": 0})
    assert r.status_code == 200 and r.json()["dialog"] is False and "Permissions" in r.json()["screen"]


def test_claude_command_choices_and_the_default_warning(lite_client, fake_tmux, tui):
    tui("claude")
    session(fake_tmux, "claude", {"model": "Opus 5", "effort": "high"})
    post = lambda body: lite_client.post(f"/api/sessions/{NAME}/command", headers=H, json=body)   # noqa: E731
    assert post({"cmd": "effort", "arg": "bogus"}).status_code == 400
    assert post({"cmd": "fast", "arg": "toggle"}).status_code == 400
    assert post({"cmd": "effort", "arg": "high"}).json()["saves_default"] is True, "typed inline it saves the default: the UI says so"
    assert post({"cmd": "effort", "arg": "ultracode off"}).json()["saves_default"] is False
    r = post({"cmd": "usage", "wait_ms": 0}).json()
    assert r["dialog"] is True
    assert post({"cmd": "context", "wait_ms": 0}).json()["dialog"] is False, "/context prints inline: nothing to Escape"


# ================================================================ pickers.py, pure

def test_rows_and_locate_read_the_bottom_picker():
    screen = "\n".join(["1. Low  old text", "› 2. Medium", "  Select Reasoning Level for gpt-6-luna", "  1. Low", "› 2. Medium (default)",
                        "  3. High", "  4. Extra high", "  5. More reasoning…   Max consumes usage limits faster"])
    found, marked = pickers.locate(screen, ["Low", "Medium", "High", "Extra high", "More reasoning"])
    assert found == {"Low": 1, "Medium": 2, "High": 3, "Extra high": 4, "More reasoning": 5} and marked == 2
    assert pickers.locate("nothing here", ["Low"]) == ({}, None)


def test_the_tables_hold_the_box_facts():
    assert pickers.CLAUDE_SLIDER == ("low", "medium", "high", "xhigh", "max")
    assert [label for _, label in pickers.CODEX_PERMS] == ["Ask for approval", "Approve for me"]
    assert "Full Access" in pickers.CODEX_PERMS_ALL and "Full Access" not in dict(pickers.CODEX_PERMS).values()
    assert pickers.PROVEN == {("claude", "effort"): False, ("claude", "ultracode"): True, ("codex", "model"): True,
                              ("codex", "reasoning"): False, ("codex", "permissions"): False}
    with pytest.raises(pickers.Refused):
        pickers.claude_ultracode("toggle")


def test_the_slash_tables_are_pinned_to_the_box_verdicts():
    """tests/fixtures/tui_verdicts_v8.json is the V8 / V8-Codex verdict table (transcribed from issues #16 and #28): every SlashSpec claim
    follows it. A command the box found absent is never offered; nothing the box saw take no argument is typed with one."""
    import json
    from pathlib import Path
    from app import agents
    v = json.loads((Path(__file__).parent / "fixtures" / "tui_verdicts_v8.json").read_text())
    cx, cl = agents.get("codex").slash_commands(), agents.get("claude").slash_commands()
    vc, vl = v["codex"]["commands"], v["claude"]["commands"]
    assert not set(v["codex"]["absent"]) & {s.cmd for s in cx.values()}
    assert vc["/model"]["inline_arg"] is False and all(not s.arg and s.drive == "picker" for k, s in cx.items() if s.cmd == "/model")
    assert vc["/permissions"]["entries"][:2] == [label for _, label in pickers.CODEX_PERMS] and vc["/permissions"]["entries"][2] == "Full Access"
    assert vc["/status"]["prints_inline"] and cx["status"].read and not cx["status"].dialog
    assert "fast" not in cx, vc["/fast"]["bare"]
    assert (cx["model"].verified, cx["reasoning"].verified, cx["permissions"].verified) == (True, False, False)
    assert v["codex"]["messages"]["model_session_only"] and pickers.CODEX_CHANGED_RE.search(v["codex"]["messages"]["model_session_only"])
    assert pickers.CODEX_STATUS_PERMS["auto"].search(v["codex"]["messages"]["status_permissions"])
    assert vl["/effort <level>"]["saves_default"] and cl["effort"].saves_default and cl["effort"].tune == "effort"
    assert vl["/model <alias>"]["saves_default"] and cl["model"].saves_default
    assert pickers.ULTRA_ON_RE.search(vl["/effort ultracode [on]"]["message"]) and pickers.ULTRA_OFF_RE.search(vl["/effort ultracode off"]["message"])
    assert "swallows" in vl["/fast"]["bare"] and cl["fast"].arg and cl["fast"].choices == ["on", "off"]
    assert [k for k, s in cl.items() if s.dialog] == [k for k in ("usage", "status", "cost") if vl["/" + k]["dialog"]]
    assert vl["/context"]["prints_inline"] and not cl["context"].dialog
    assert vl["/effort"]["ran_end_to_end"] != True and pickers.PROVEN[("claude", "effort")] is False   # noqa: E712
