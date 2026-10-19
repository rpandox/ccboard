"""scripts/dev/pools_spike.py (issue #219): the one-command spike for two logins.

Everything here is a dry run, or a run with a scripted stand-in for the two CLIs (the Runner is replaced; no claude, codex, security or
network call is made, and subprocess is made to fail loudly if anything tries). HOME is the per-test temp dir (tests/conftest.py), so
"the real ~/.claude" in these tests is a folder under tmp_path and the owner's own is never touched or read.
"""
import builtins
import importlib.util
import io
import json
import os
import re
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "dev" / "pools_spike.py"
CREDENTIAL_NAMES = {".credentials.json", "auth.json", ".claude.json"}
SENTINEL = b"SENTINEL-CREDENTIAL-BYTES"


def _load():
    spec = importlib.util.spec_from_file_location("pools_spike_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ps = _load()


@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    """No process, no network, no inherited login variable; a home under tmp_path."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in ps.SCRUB:
        monkeypatch.delenv(var, raising=False)

    def boom(*a, **k):
        raise AssertionError(f"a test started a process: {a[:1]}")

    monkeypatch.setattr(ps.subprocess, "run", boom)
    monkeypatch.setattr(ps.subprocess, "Popen", boom)
    return home


def run_main(tmp_path, *extra, ask=None):
    out: list[str] = []
    base = tmp_path / "base"
    base.mkdir(exist_ok=True)
    args = list(extra)
    if "--base" not in args:
        args += ["--base", str(base)]
    rc = ps.main(args, say=out.append, ask=ask or (lambda prompt: ""))
    return rc, "\n".join(out)


def scratch_of(text) -> Path:
    m = re.search(r"Scratch tree \(holds real logins until you delete it\): (\S+)", text)
    assert m, text
    return Path(m.group(1))


# ---------------------------------------------------------------- refusing the real homes
@pytest.mark.parametrize("sub", [".claude", ".codex", ".claude/projects", ".codex/sessions"])
def test_it_refuses_a_base_inside_a_real_home(tmp_path, _sandbox, sub):
    real = _sandbox / sub
    real.mkdir(parents=True)
    before = sorted(p.name for p in real.iterdir())
    rc, text = run_main(tmp_path, "--dry-run", "--base", str(real))
    assert rc == 2 and "refusing to run" in text
    assert sorted(p.name for p in real.iterdir()) == before, "nothing is created in a real home"


def test_it_refuses_a_base_that_is_a_symlink_into_a_real_home(tmp_path, _sandbox):
    (_sandbox / ".claude").mkdir()
    link = tmp_path / "innocent"
    link.symlink_to(_sandbox / ".claude", target_is_directory=True)
    rc, text = run_main(tmp_path, "--dry-run", "--base", str(link))
    assert rc == 2 and "refusing to run" in text
    assert list((_sandbox / ".claude").iterdir()) == []


def test_it_refuses_where_the_callers_environment_already_points(tmp_path, monkeypatch):
    elsewhere = tmp_path / "their-codex-home"
    elsewhere.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(elsewhere))
    rc, text = run_main(tmp_path, "--dry-run", "--base", str(elsewhere))
    assert rc == 2 and "refusing to run" in text


def test_a_base_that_merely_contains_the_home_is_fine(tmp_path, _sandbox):
    rc, text = run_main(tmp_path, "--dry-run", "--base", str(_sandbox.parent))
    assert rc == 0 and scratch_of(text).parent == _sandbox.parent.resolve()


def test_the_runner_refuses_to_start_anything_pointed_at_a_real_home(tmp_path, _sandbox):
    runner = ps.Runner(True, lambda s: None)
    with pytest.raises(ps.Refused):
        runner.run(["claude", "-p", "x"], {"CLAUDE_CONFIG_DIR": str(_sandbox / ".claude")})
    with pytest.raises(ps.Refused):
        runner.run(["codex", "exec", "x"], {"CODEX_HOME": str(_sandbox / ".codex" / "sub")})
    runner.run(["codex", "exec", "x"], {"CODEX_HOME": str(tmp_path / "scratch")})        # a scratch home passes


def test_children_never_inherit_a_login_variable(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY", "CODEX_ACCESS_TOKEN", "CODEX_HOME", "ANTHROPIC_FEDERATION_X"):
        monkeypatch.setenv(var, "x")
    monkeypatch.setenv("PATH_MARKER", "kept")
    env = ps.Runner.child_env({"CODEX_HOME": "/scratch/a"})
    assert env["CODEX_HOME"] == "/scratch/a" and env["PATH_MARKER"] == "kept"
    assert not [k for k in env if k in ps.SCRUB and k != "CODEX_HOME"] and "ANTHROPIC_FEDERATION_X" not in env


# ---------------------------------------------------------------- the layout
def test_dry_run_builds_the_scratch_tree(tmp_path):
    rc, text = run_main(tmp_path, "--dry-run")
    assert rc == 0
    root = scratch_of(text)
    assert root == root.resolve() and root.parent == (tmp_path / "base").resolve()
    for side in "AB":
        assert (root / "claude" / side / "projects").is_symlink()
        assert (root / "claude" / side / "projects").resolve() == (root / "shared" / "claude" / "projects").resolve()
        assert (root / "codex" / side / "sessions").is_symlink()
        assert (root / "codex" / side / "sessions").resolve() == (root / "shared" / "codex" / "sessions").resolve()
        assert 'cli_auth_credentials_store = "file"' in (root / "codex" / side / "config.toml").read_text().splitlines()
        assert not (root / "claude" / side / ".credentials.json").exists() and not (root / "codex" / side / "auth.json").exists()
    assert (root / "shared" / "codex" / "sqlite").is_dir() and (root / "work").is_dir() and (root / "results").is_dir()


def test_separate_sqlite_variant_gives_each_codex_profile_its_own_database_home(tmp_path):
    rc, text = run_main(tmp_path, "--dry-run", "--codex-sqlite", "separate")
    root = scratch_of(text)
    assert rc == 0 and not (root / "shared" / "codex" / "sqlite").exists()
    assert "CODEX_SQLITE_HOME=" not in text and "shared sessions/ and separate SQLite home" in text


def test_only_limits_the_run_to_one_provider(tmp_path):
    _, text = run_main(tmp_path, "--dry-run", "--only", "codex")
    assert "codex exec" in text and "claude -p" not in text and "== Claude" not in text
    _, text = run_main(tmp_path, "--dry-run", "--only", "claude")
    assert "claude -p" in text and "codex exec" not in text and "== Codex" not in text


# ---------------------------------------------------------------- the commands it prints
def test_dry_run_prints_every_step_and_the_login_commands(tmp_path):
    rc, text = run_main(tmp_path, "--dry-run")
    root = scratch_of(text)
    q = ps.shlex.quote
    a, b = q(str(root / "claude" / "A")), q(str(root / "claude" / "B"))
    assert f"CLAUDE_CONFIG_DIR={a} claude auth login" in text and f"CLAUDE_CONFIG_DIR={b} claude auth login" in text
    ca, cb = q(str(root / "codex" / "A")), q(str(root / "codex" / "B"))
    assert f"CODEX_HOME={ca} codex login --device-auth" in text and f"CODEX_HOME={cb} codex login --device-auth" in text
    lines = [l for l in text.splitlines() if l.startswith("  $ ")]
    claude_turns = [l for l in lines if " claude -p " in l and "--output-format json" in l]
    assert len(claude_turns) == 5 and "--session-id" in claude_turns[0] and all("--resume" in l for l in claude_turns[1:4])
    assert f"CLAUDE_CONFIG_DIR={a} claude -p" in claude_turns[0] and f"CLAUDE_CONFIG_DIR={b} claude -p" in claude_turns[3]
    assert "What was the word I gave you in turn 1?" in claude_turns[3] and "/usage" in claude_turns[4]
    codex_turns = [l for l in lines if " codex exec" in l]
    assert len(codex_turns) == 4 and " codex exec resume " not in codex_turns[0] and all(" codex exec resume " in l for l in codex_turns[1:])
    sqlite = q(str(root / "shared" / "codex" / "sqlite"))
    assert all(f"CODEX_SQLITE_HOME={sqlite}" in l for l in codex_turns)
    assert f"CODEX_HOME={cb} " in codex_turns[3] and "What was the word I gave you in turn 1?" in codex_turns[3]
    assert any("codex app-server" in l and "account/rateLimits/read" in l for l in lines)
    assert any(l.strip().endswith("claude auth status") for l in lines) and any(l.strip().endswith("codex login status") for l in lines)
    assert "(dry run: not waiting)" in text and "rm -rf " in text
    assert 'security find-generic-password -s "Claude Code-credentials-' in text and "never queries the Keychain" in text


def test_dry_run_never_waits_for_the_owner(tmp_path):
    def no_input(prompt):
        raise AssertionError("a dry run must not wait for Enter")

    assert run_main(tmp_path, "--dry-run", ask=no_input)[0] == 0


def test_the_keychain_names_follow_the_researched_formulas(tmp_path):
    layout = ps.build_layout(tmp_path / "s", "shared") if (tmp_path / "s").mkdir() is None else None
    names = ps.keychain_names(layout)
    d = str(layout["claude_A"])
    assert names["claude"]["A"] == "Claude Code-credentials-" + ps.hashlib.sha256(d.encode()).hexdigest()[:8]
    assert re.fullmatch(r"cli\|[0-9a-f]{16}", names["codex"]["B"]) and names["codex"]["A"] != names["codex"]["B"]


# ---------------------------------------------------------------- credentials are opaque
def _forbid_credential_reads(monkeypatch):
    """Opening a credential file for reading fails the test (the scripted CLIs may still write one, as a login would)."""
    real_open = builtins.open

    def refuse(path, mode="r"):
        name = os.path.basename(os.fspath(path)) if isinstance(path, (str, bytes, os.PathLike)) else ""
        if name in CREDENTIAL_NAMES and not set(str(mode)) & set("wax"):
            raise AssertionError(f"read of a credential file: {name}")

    def guarded_open(path, mode="r", *a, **k):
        refuse(path, mode)
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_open)
    real_read_text, real_read_bytes = Path.read_text, Path.read_bytes

    def read_text(self, *a, **k):
        refuse(self)
        return real_read_text(self, *a, **k)

    def read_bytes(self, *a, **k):
        refuse(self)
        return real_read_bytes(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)


def test_a_dry_run_reads_no_credential_file_even_when_one_exists(tmp_path, monkeypatch, _sandbox):
    for d in (".claude", ".codex"):
        (_sandbox / d).mkdir()
    (_sandbox / ".claude" / ".credentials.json").write_bytes(SENTINEL)
    (_sandbox / ".claude.json").write_bytes(SENTINEL)
    (_sandbox / ".codex" / "auth.json").write_bytes(SENTINEL)
    _forbid_credential_reads(monkeypatch)
    rc, text = run_main(tmp_path, "--dry-run")
    assert rc == 0 and SENTINEL.decode() not in text
    for f in scratch_of(text).rglob("*"):
        if f.is_file() and not f.is_symlink():
            assert SENTINEL not in f.read_bytes()


# ---------------------------------------------------------------- a scripted run
class FakeClis:
    """Stands in for both CLIs. Keeps the transcript where the real ones would put it (through the profile's symlink)."""

    def __init__(self, recall_ok=True):
        self.recall_ok, self.calls, self.word, self.thread = recall_ok, [], None, str(uuid.uuid4())

    def out(self, stdout="", rc=0, stderr=""):
        return {"rc": rc, "stdout": stdout, "stderr": stderr, "timeout": False}

    def run(self, argv, extra, cwd=None, timeout=0):
        self.calls.append((argv, extra))
        tool = argv[0]
        if argv[1] == "--version":
            return self.out("2.1.303 (Claude Code)\n" if tool == "claude" else "codex-cli 0.162.1\n")
        if tool == "claude" and argv[1:3] == ["auth", "status"]:
            side = "A" if extra["CLAUDE_CONFIG_DIR"].endswith("/A") else "B"
            (Path(extra["CLAUDE_CONFIG_DIR"]) / ".credentials.json").write_bytes(SENTINEL)      # "the login"
            return self.out(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "email": f"person-{side}@example.test", "orgName": f"Org {side}",
                                        "orgId": str(uuid.uuid4()), "subscriptionType": "max"}))
        if tool == "claude":
            prompt = argv[2]
            if prompt == "/usage":
                return self.out(json.dumps({"result": "Current session: 12% used. Weekly: 40% used.", "is_error": False}))
            sid = argv[argv.index("--session-id") + 1] if "--session-id" in argv else argv[argv.index("--resume") + 1]
            if "secret word" in prompt:
                self.word = re.search(r"later: (\S+)\.", prompt).group(1)
                d = Path(extra["CLAUDE_CONFIG_DIR"]) / "projects" / "-work"
                d.mkdir(exist_ok=True)
                (d / f"{sid}.jsonl").write_text("{}\n")
            answer = {"result": self.word if ("turn 1" in prompt and self.recall_ok) else "OK", "session_id": sid, "is_error": False,
                      "usage": {"input_tokens": 3, "cache_creation_input_tokens": 90000, "cache_read_input_tokens": 0, "output_tokens": 5, "service_tier": "x"},
                      "total_cost_usd": 0.01}
            return self.out(json.dumps(answer))
        if argv[1:3] == ["login", "status"]:
            (Path(extra["CODEX_HOME"]) / "auth.json").write_bytes(SENTINEL)
            return self.out("Logged in using ChatGPT\n")
        o = Path(argv[argv.index("-o") + 1])
        if "secret word" in argv[-1]:
            self.word = re.search(r"later: (\S+)\.", argv[-1]).group(1)
            d = Path(extra["CODEX_HOME"]) / "sessions" / "2026" / "10" / "19"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"rollout-2026-10-19T00-00-00-{self.thread}.jsonl").write_text("{}\n")
            o.write_text("OK")
            return self.out(json.dumps({"type": "thread.started", "thread_id": self.thread}) + "\n")
        o.write_text(self.word if ("turn 1" in argv[-1] and self.recall_ok) else "OK")
        return self.out("{}\n")

    def rpc(self, argv, extra, messages, want_id, timeout=30):
        self.calls.append((argv, extra))
        return {"id": 2, "result": {"rateLimits": {"primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 1760000000},
                                                  "secondary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": 1760500000}, "planType": "plus",
                                                  "email": "leak@example.test"}}}


@pytest.mark.parametrize("recall_ok", [True, False])
def test_a_scripted_run_records_context_accounts_and_usage_without_secrets(tmp_path, monkeypatch, recall_ok):
    fake = FakeClis(recall_ok)
    monkeypatch.setattr(ps.Runner, "run", lambda self, argv, extra, cwd=None, timeout=0: (self._guard(extra), fake.run(argv, extra, cwd))[1])
    monkeypatch.setattr(ps.Runner, "rpc", lambda self, argv, extra, messages, want_id, timeout=30: (self._guard(extra), fake.rpc(argv, extra, messages, want_id))[1])
    monkeypatch.setattr(ps.shutil, "which", lambda name: f"/fake/{name}")
    _forbid_credential_reads(monkeypatch)
    asked: list[str] = []
    rc, text = run_main(tmp_path, ask=lambda prompt: asked.append(prompt) or "")
    assert rc == 0
    assert sum("is logged in" in a for a in asked) == 4, "one Enter per profile"
    root = scratch_of(text)
    res = json.loads((root / "results" / "results.json").read_text())
    assert res["versions"] == {"claude": "2.1.303 (Claude Code)", "codex": "codex-cli 0.162.1"}
    for key in ("claude", "codex"):
        r = res[key]
        assert r["status"] == "ok" and r["context_present"] is recall_ok
        assert r["shared_transcript_present"] is True and r["real_home_transcript_present"] is False
        assert [t["profile"] for t in r["turns"]] == ["A", "A", "A", "B"]
        assert r["login"]["A"]["credentials_file_exists"] is True
    assert res["claude"]["accounts_differ"] is True and res["codex"]["accounts_differ"] is None, "no address in Codex's status line: cannot tell"
    assert res["claude"]["login"]["A"]["account"] == "p***@e***.test" and res["claude"]["account_b_after_resume"]["account"] == "p***@e***.test"
    assert res["claude"]["turns"][3]["usage"]["cache_creation_input_tokens"] == 90000 and "service_tier" not in res["claude"]["turns"][3]["usage"]
    assert res["codex"]["usage_app_server"]["result"]["rateLimits"]["primary"] == {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 1760000000}
    assert "email" not in json.dumps(res["codex"]["usage_app_server"]) and res["codex"]["usage_app_server"]["result"]["rateLimits"]["planType"] == "plus"
    everything = text + "".join(f.read_text() for f in (root / "results").glob("*.*"))
    for leak in ("person-A@", "person-B@", "leak@", "Org A", "Org B", SENTINEL.decode(), fake.thread, "orgId"):
        assert leak not in everything, leak
    assert "context present): " + ("yes" if recall_ok else "no") in (root / "results" / "summary.md").read_text()


# ---------------------------------------------------------------- what the files may contain
SECRET_SHAPES = [r"\bsk-[A-Za-z0-9_-]{8,}", r"\beyJ[A-Za-z0-9_-]{6,}", r"Bearer\s+\S+", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}",
                 r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", r"\b(?=[A-Za-z0-9_.\-]*\d)[A-Za-z0-9_.\-]{32,}\b",
                 r"access_token|refresh_token|id_token|accountUuid|organizationUuid"]


def test_the_written_reports_hold_nothing_secret_shaped(tmp_path):
    rc, text = run_main(tmp_path, "--dry-run")
    results = scratch_of(text) / "results"
    assert sorted(p.name for p in results.iterdir()) == ["issue-comment.md", "results.json", "summary.md"]
    for f in results.iterdir():
        body = f.read_text()
        for shape in SECRET_SHAPES:
            assert not re.search(shape, body), (f.name, shape)
    assert "DRY RUN" in (results / "summary.md").read_text() and json.loads((results / "results.json").read_text())["dry_run"] is True


def test_scrub_removes_addresses_ids_and_token_shapes():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc"
    text = f"a.b@mail.example 123e4567-e89b-12d3-a456-426614174000 Bearer abc.def sk-abcdefgh12345678 {jwt} {'x1' * 20} /scratch/root/claude/A"
    out = ps.scrub(text, {"/scratch/root": "<scratch>"})
    assert "a***@m***.example" in out and "123e4567..." in out and "<scratch>/claude/A" in out
    for gone in ("a.b@", "e89b", "abcdefgh12345678", "eyJhbGci", "x1x1x1x1x1"):
        assert gone not in out
    assert ps.mask_email("nodot@host") == "n***" and ps.mask_word("") == ""


def test_the_script_makes_no_http_call_of_its_own():
    src = SCRIPT.read_text()
    for needle in ("urllib", "http.client", "requests", "socket", "aiohttp", "urlopen", ".credentials.json\").read", "keyring"):
        assert needle not in src, needle
    assert not re.search(r"\b(?:security|find-generic-password)\b.*subprocess|\[\"security\"", src), "the Keychain is only ever named in text for the owner"


def test_real_home_changes_are_reported_by_name_from_stat_only(tmp_path, _sandbox):
    (_sandbox / ".claude").mkdir()
    before = ps.snapshot(ps.real_watch_paths())
    assert ps.changed(before, ps.snapshot(ps.real_watch_paths())) == []
    (_sandbox / ".claude" / "history.jsonl").write_text("{}")
    assert ".claude/history.jsonl" in ps.changed(before, ps.snapshot(ps.real_watch_paths()))
