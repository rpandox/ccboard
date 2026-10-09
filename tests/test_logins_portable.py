"""Issue #119, Claude and Codex logins off Linux: account_store.support_reason() per system, the macOS pin (nothing is written, the live
credentials file is byte-identical), the Keychain probe (attributes only, never -w or -g), the status cache key, Codex's credential store
(credential_store(), supported(), the 409 and the doctor row) and the doctor's claude-creds-store.

Everything runs on temp directories and fakes: the OS is switched by the platform module's flags (the one place for OS questions), `security`
is a script on a temp PATH that refuses -w and -g by printing a sentinel and failing, `codex` is a fake object or a fake script, and nothing
reads or writes the real ~/.claude, ~/.codex, Keychain or tmux socket. Credentials are opaque bytes with a sentinel in them.
"""
import hashlib
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import account_store, claude_auth, codex_accounts as cx, doctor
from app import platform as plat
from app.config import settings
from app.db import DB

REAL_SUPPORT_REASON = account_store.support_reason          # captured at import, before the suite's autouse fixture patches it off
REAL_UNDER_DRVFS = plat.under_drvfs
H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
UA = "aaaaaaaa-0000-4000-8000-000000000001"
LIVE_BLOB = b'{"claudeAiOauth":{"accessToken":"SECRET-LIVE-FILE"}}'
LEAK = "LEAKED-SECRET-XYZ"

OLD_REASON = "saved logins need Claude's file credentials (Linux)"
MAC_REASON = "Claude Code keeps this login in the macOS Keychain, so the board cannot swap it; Codex logins can still be saved"
WIN_REASON = "Windows is not a supported board host (use WSL2)"
MODES_REASON = "the Claude config folder is on a file system that ignores permissions; move it into the Linux file system"
KEYRING_TEXT = 'Codex keeps this login in the system keyring; set cli_auth_credentials_store = "file" in config.toml and log in again to use saved logins'


def set_system(monkeypatch, system: str, drvfs: bool = False):
    """linux | macos | windows | other: the platform module's flags (and whether the Claude config dir counts as a Windows drive)."""
    monkeypatch.setattr(plat, "IS_LINUX", system == "linux")
    monkeypatch.setattr(plat, "IS_MACOS", system == "macos")
    monkeypatch.setattr(plat, "IS_WINDOWS", system == "windows")
    monkeypatch.setattr(plat, "under_drvfs", lambda p: drvfs)
    # the suite's default puts supported() at False; here the real rules are back
    monkeypatch.setattr(account_store, "support_reason", REAL_SUPPORT_REASON)
    monkeypatch.setattr(account_store, "supported", lambda: REAL_SUPPORT_REASON() is None)


def tree(root: Path) -> dict:
    """Every path under root with what would change if it were written: size, mtime, mode and the bytes' hash."""
    out = {}
    for p in sorted(root.rglob("*")) if root.exists() else []:
        st = p.lstat()
        out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns, stat.S_IMODE(st.st_mode),
                                         hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
    return out


@pytest.fixture
def home(projects_dir, tmp_path, monkeypatch):
    """The sandboxed config dir with a live credentials file and a state file, the sandboxed data dir, a DB. Nothing is the real home."""
    monkeypatch.setattr(settings, "backup_extra", [])
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    live = settings.claude_config_dir
    live.mkdir(parents=True, exist_ok=True)
    assert tmp_path in live.parents and tmp_path in settings.data_dir.parents, "the sandbox is under the test's temp dir"
    creds = live / ".credentials.json"
    creds.write_bytes(LIVE_BLOB)
    os.chmod(creds, 0o600)
    (live / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": UA, "emailAddress": "a@example.com", "displayName": "Ann"}}))
    return SimpleNamespace(live=live, creds=creds, data=settings.data_dir, db=DB(settings.db_path), tmp=tmp_path)


# ---------------------------------------------------------------- support_reason(): one text per system

def test_the_linux_text_is_the_old_one_and_the_new_texts_are_pinned():
    assert account_store.REASON == OLD_REASON
    assert (account_store.REASON_MACOS, account_store.REASON_WINDOWS, account_store.REASON_MODES) == (MAC_REASON, WIN_REASON, MODES_REASON)


def test_support_reason_per_system(monkeypatch):
    for system, want in (("linux", None), ("macos", MAC_REASON), ("windows", WIN_REASON), ("other", OLD_REASON)):
        set_system(monkeypatch, system)
        assert account_store.support_reason() == want, system
        assert account_store.supported() is (want is None), system                 # supported() stays a boolean
        assert account_store.why_not() == (want or OLD_REASON)


def test_wsl_on_ext4_is_linux_and_a_config_dir_on_a_windows_drive_is_refused_with_its_own_reason(monkeypatch):
    set_system(monkeypatch, "linux", drvfs=False)
    assert account_store.support_reason() is None and account_store.supported() is True
    set_system(monkeypatch, "linux", drvfs=True)
    assert account_store.support_reason() == MODES_REASON and account_store.supported() is False


def test_the_real_under_drvfs_decides_the_modes_reason(monkeypatch, tmp_path):
    set_system(monkeypatch, "linux")
    monkeypatch.setattr(plat, "under_drvfs", REAL_UNDER_DRVFS)
    monkeypatch.setattr(plat, "is_wsl", lambda: True)
    monkeypatch.setattr(settings, "claude_config_dir", Path("/mnt/d/data/.claude"))
    assert account_store.support_reason() == MODES_REASON
    monkeypatch.setattr(settings, "claude_config_dir", tmp_path / "claude")
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    assert account_store.support_reason() is None
    monkeypatch.setattr(plat, "is_wsl", lambda: False)
    monkeypatch.setattr(settings, "claude_config_dir", Path("/mnt/d/data/.claude"))
    assert account_store.support_reason() is None, "off WSL a path under /mnt is just a path"



# ---------------------------------------------------------------- macOS: nothing is written, the live file is untouched

MUTATORS = (
    ("save_live", lambda db: account_store.save_live(db, UA)),
    ("seed_current", lambda db: account_store.seed_current(db)),
    ("switch", lambda db: account_store.switch(db, UA)),
    ("forget", lambda db: account_store.forget(db, UA)),
    ("start_login", lambda db: account_store.start_login(db)),
)


@pytest.mark.parametrize("system,reason", [("macos", MAC_REASON), ("windows", WIN_REASON)])
def test_off_linux_every_mutating_call_raises_and_nothing_is_written(home, monkeypatch, system, reason):
    set_system(monkeypatch, system)
    before_live, before_data = tree(home.live), tree(home.data)
    for name, call in MUTATORS:
        with pytest.raises(account_store.Unsupported) as e:
            call(home.db)
        assert str(e.value) == reason, name
    assert account_store.finalize(home.db) is None and account_store.tick(home.db) is None
    assert home.creds.read_bytes() == LIVE_BLOB, "the live credentials file is byte-identical"
    assert tree(home.live) == before_live, "nothing under the config dir was written, touched or created"
    assert tree(home.data) == before_data, "nothing under the data dir either (no slot, no .pending)"
    assert not (home.data / "accounts").exists()
    d = account_store.decorate({"current": UA, "list": [{"key": UA}]}, home.db)
    assert d["store"] == {"supported": False, "reason": reason, "count": 0} and d["list"][0]["saved"] is False


def test_every_mutator_is_behind_the_support_check():
    """A later change to supported() cannot open a back door: each public writer starts with _require(), the two loops check supported()."""
    import inspect
    for fn in (account_store.save_live, account_store.seed_current, account_store.switch, account_store.forget, account_store.start_login):
        assert "_require()" in inspect.getsource(fn), fn.__name__
    for fn in (account_store.finalize, account_store.tick):
        assert "if not supported()" in inspect.getsource(fn), fn.__name__


def test_the_only_credential_store_is_the_file_one(home):
    assert account_store.CredentialStore.__subclasses__() == [account_store.FileCredentialStore]
    store = account_store.FileCredentialStore(home.creds)
    assert store.read_login() == LIVE_BLOB and store.stamp() == account_store._stamp(home.creds)
    other = account_store.FileCredentialStore(home.tmp / "elsewhere.json")
    assert other.read_login() is None and other.stamp() is None
    other.write_login(b"opaque")
    assert other.path.read_bytes() == b"opaque" and stat.S_IMODE(other.path.stat().st_mode) == 0o600
    with pytest.raises(NotImplementedError):
        account_store.CredentialStore().read_login()


def test_secure_write_keeps_the_old_write_atomic_mode_and_content(tmp_path):
    dest = tmp_path / "x" / "creds.json"
    dest.parent.mkdir()
    account_store._write_atomic(dest, b"one", 0o600)
    assert dest.read_bytes() == b"one" and stat.S_IMODE(dest.stat().st_mode) == 0o600
    account_store._write_atomic(dest, b"two", 0o640)
    assert dest.read_bytes() == b"two" and stat.S_IMODE(dest.stat().st_mode) == 0o640
    assert [p.name for p in dest.parent.iterdir()] == ["creds.json"], "no temp file left"


# ---------------------------------------------------------------- the API answers carry the same per-system text

@pytest.mark.parametrize("system,reason,drvfs", [("macos", MAC_REASON, False), ("windows", WIN_REASON, False), ("linux", MODES_REASON, True)])
def test_the_409_and_the_view_carry_the_per_system_reason(home, lite_client, monkeypatch, system, reason, drvfs):
    set_system(monkeypatch, system, drvfs=drvfs)
    before = (tree(home.live), tree(home.data))
    r = lite_client.post("/api/accounts/login", headers=H, json={})
    assert r.status_code == 409 and r.json() == {"detail": reason, "error": reason}
    r = lite_client.post(f"/api/accounts/{UA}/switch", headers=H, json={})
    assert r.status_code == 409 and r.json()["detail"] == reason
    r = lite_client.delete(f"/api/accounts/{UA}/saved", headers=H)
    assert r.status_code == 409 and r.json()["detail"] == reason
    st = lite_client.get("/api/state", headers=H).json()
    assert st["accounts"]["store"] == {"supported": False, "reason": reason, "count": 0}
    assert lite_client.get("/api/accounts", headers=H).json()["store"] == {"supported": False, "reason": reason, "count": 0}
    assert (tree(home.live), tree(home.data)) == before, "no request changed a file under the config dir or the data dir"
    assert home.creds.read_bytes() == LIVE_BLOB


def test_the_web_login_route_does_not_depend_on_the_store(home, lite_client, fake_tmux, monkeypatch):
    """`Log in` without switching: POST /api/claude/login runs claude auth login in the login session (no saved-login check), with the BROWSER stub."""
    set_system(monkeypatch, "macos")
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/bin/claude")
    monkeypatch.setattr(plat, "browser_stub", lambda: "/stub/true")
    r = lite_client.post("/api/claude/login", headers=H, json={})
    assert r.status_code == 200 and r.json() == {"ok": True}
    name, _cwd, env = fake_tmux["created"][-1]
    assert env == {"BROWSER": "/stub/true"}, "the login env has the stub and no CLAUDE_CONFIG_DIR: the live config dir is the one logged in"
    assert fake_tmux["sent"][-1][1] == "claude auth login"
    assert home.creds.read_bytes() == LIVE_BLOB


# ---------------------------------------------------------------- the Keychain probe: attributes only

FAKE_SECURITY = r"""#!/bin/sh
# a stand-in for /usr/bin/security: logs its argv, refuses -w and -g (the secret) by printing a sentinel and failing, answers by FAKE_SECURITY_MODE
echo "$@" >> "$FAKE_SECURITY_LOG"
for a in "$@"; do
  case "$a" in
    -w|-g|-?*[wg]*) echo "LEAKED-SECRET-XYZ"; echo "LEAKED-SECRET-XYZ" >&2; exit 99 ;;
  esac
done
case "$FAKE_SECURITY_MODE" in
  present) cat "$FAKE_SECURITY_ATTRS"; exit 0 ;;
  absent) echo "security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain." >&2; exit 44 ;;
  slow) sleep 3; exit 0 ;;
  *) echo "security: something else" >&2; exit 1 ;;
esac
"""

ATTRS = """keychain: "/home/x/Library/Keychains/login.keychain-db"
version: 512
class: "genp"
attributes:
    0x00000007 <blob>="Claude Code-credentials"
    "acct"<blob>="someone"
    "cdat"<timedate>=0x32303236303130313030303030305A00  "20260101000000Z\\000"
    "mdat"<timedate>=0x32303236303130353132333030305A00  "{mdat}Z\\000"
    "svce"<blob>="Claude Code-credentials"
"""


@pytest.fixture
def sec(tmp_path, monkeypatch):
    """A fake `security` first on PATH. sec.mode(...) picks its answer, sec.calls() are the argv lines it saw."""
    bindir = tmp_path / "fakesec"
    bindir.mkdir()
    exe = bindir / "security"
    exe.write_text(FAKE_SECURITY)
    exe.chmod(0o755)
    log = tmp_path / "security.log"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}/bin{os.pathsep}/usr/bin")
    attrs = tmp_path / "security.attrs"
    monkeypatch.setenv("FAKE_SECURITY_LOG", str(log))
    monkeypatch.setenv("FAKE_SECURITY_ATTRS", str(attrs))
    monkeypatch.setattr(claude_auth, "_kc_cache", None)
    s = SimpleNamespace(log=log)

    def mode(m, mdat="20260105123000"):
        monkeypatch.setenv("FAKE_SECURITY_MODE", m)
        attrs.write_text(ATTRS.replace("{mdat}", mdat))

    s.mode = mode
    s.calls = lambda: log.read_text().splitlines() if log.exists() else []
    mode("present")
    return s


def test_keychain_item_asks_for_attributes_only(sec, monkeypatch):
    monkeypatch.setattr(plat, "IS_MACOS", True)
    got = claude_auth.keychain_item()
    assert got == {"state": "present", "modified": "20260105123000"}
    assert sec.calls() == ['find-generic-password -s Claude Code-credentials']
    seen = []
    claude_auth.keychain_item(run=lambda argv: (seen.append(list(argv)), (0, ""))[1])
    assert seen == [["security", "find-generic-password", "-s", "Claude Code-credentials"]], "the exact argv: no -w, no -g, nothing that prints the value"


def test_keychain_item_answers(sec, monkeypatch):
    monkeypatch.setattr(plat, "IS_MACOS", True)
    sec.mode("absent")
    assert claude_auth.keychain_item() == {"state": "absent", "modified": None}
    sec.mode("other")
    assert claude_auth.keychain_item() == {"state": "unknown", "modified": None}, "a failed call is unknown, not absent"
    monkeypatch.setattr(claude_auth, "KEYCHAIN_TIMEOUT", 0.3)
    sec.mode("slow")
    assert claude_auth.keychain_item() == {"state": "unknown", "modified": None}, "a timed-out call is unknown too"
    def gone(argv):
        raise OSError("no security here")
    assert claude_auth.keychain_item(run=gone) == {"state": "unknown", "modified": None}, "a missing `security` is unknown"
    assert LEAK not in "".join(sec.calls())


def test_keychain_item_runs_nothing_off_macOS(sec, monkeypatch):
    monkeypatch.setattr(plat, "IS_MACOS", False)
    assert claude_auth.keychain_item() == {"state": "unknown", "modified": None}
    assert sec.calls() == []


def test_parse_mdat_reads_that_one_attribute_only():
    text = '    "cdat"<timedate>=0x00  "20260101000000Z\\000"\n    "mdat"<timedate>=0x00  "20260105123000Z\\000"\n'
    assert claude_auth.parse_mdat(text) == "20260105123000"
    assert claude_auth.parse_mdat('    "cdat"<timedate>=0x00  "20260101000000Z\\000"') is None
    assert claude_auth.parse_mdat("") is None and claude_auth.parse_mdat(None) is None


# ---------------------------------------------------------------- the status cache key

def test_creds_key_on_linux_is_the_old_tuple_and_never_runs_security(sec, home, monkeypatch):
    set_system(monkeypatch, "linux")
    st = home.creds.stat()
    assert claude_auth._creds_key() == (True, st.st_mtime_ns, st.st_size)
    home.creds.unlink()
    assert claude_auth._creds_key() == (False, 0, 0)
    assert sec.calls() == []


def test_creds_key_on_macos_follows_the_keychain_items_modification_date(sec, home, monkeypatch):
    set_system(monkeypatch, "macos")
    now = [1000.0]
    monkeypatch.setattr(claude_auth, "_kc_clock", lambda: now[0])
    st = home.creds.stat()
    base = (True, st.st_mtime_ns, st.st_size)
    k1 = claude_auth._creds_key()
    assert k1 == (*base, "present", "20260105123000")
    sec.mode("present", "20260106090000")
    now[0] += 10
    assert claude_auth._creds_key() == k1 and len(sec.calls()) == 1, "within 30 s: the cached answer, no second call"
    now[0] += 25
    k2 = claude_auth._creds_key()
    assert k2 == (*base, "present", "20260106090000") and k2 != k1 and len(sec.calls()) == 2, "after 30 s the new date is in the key"
    sec.mode("present", "20260107090000")
    claude_auth.invalidate()
    assert claude_auth._creds_key()[-1] == "20260107090000", "invalidate() forgets the cached answer (a login just happened)"
    assert all("-w" not in c.split() and "-g" not in c.split() for c in sec.calls())


# ---------------------------------------------------------------- doctor: claude-creds-store

def run_check(home, sec_state, mode, with_file, monkeypatch):
    set_system(monkeypatch, "macos")
    monkeypatch.setattr(doctor, "CMD_TIMEOUT", 0.4)
    if not with_file:
        home.creds.unlink()
    sec_state.mode(mode)
    return doctor._c_claude_creds_store(None)


@pytest.mark.parametrize("mode,with_file,status,word", [
    ("present", True, "warn", "both"),
    ("present", False, "pass", "keychain"),
    ("absent", True, "pass", "file"),
    ("absent", False, "warn", "none"),
    ("other", True, "warn", "unknown"),
    ("other", False, "warn", "unknown"),
    ("slow", False, "warn", "unknown"),
])
def test_doctor_claude_creds_store(home, sec, monkeypatch, caplog, mode, with_file, status, word):
    with caplog.at_level("DEBUG"):
        out = run_check(home, sec, mode, with_file, monkeypatch)
    assert out.status == status and out.detail.startswith(word + ":"), out
    assert out.fix is not None and out.fix["text"]
    blob = json.dumps([out.detail, out.fix]) + caplog.text
    assert LEAK not in blob and "SECRET" not in blob, "no secret in the outcome or the log"
    assert sec.calls() and all("-w" not in c.split() and "-g" not in c.split() for c in sec.calls()), "never -w or -g"
    if word == "both":
        assert "stale second copy" in out.detail and "the board never does" in out.fix["text"] and home.creds.exists(), "the file is not deleted"
    if word == "unknown":
        assert "none" not in out.detail.split(":")[0], "a failed or timed-out call is never read as none"


def test_doctor_claude_creds_store_is_macos_only(sec, home, monkeypatch):
    set_system(monkeypatch, "linux")
    out = doctor._c_claude_creds_store(None)
    assert out.status == "skip" and sec.calls() == []


def test_the_secret_is_not_in_the_doctor_api_answer_either(home, sec, lite_client, monkeypatch):
    set_system(monkeypatch, "macos")
    monkeypatch.setattr(doctor, "CHECKS", list(doctor.CHECKS))
    monkeypatch.setattr(doctor, "GROUPS", list(doctor.GROUPS))
    doctor.register("claude-creds-store", "claude", "Claude login store", doctor._c_claude_creds_store)
    doctor.invalidate()
    r = lite_client.get("/api/doctor?group=claude&refresh=1", headers=H)
    assert r.status_code == 200
    row = next(c for c in r.json()["checks"] if c["id"] == "claude-creds-store")
    assert row["status"] == "warn" and row["detail"].startswith("both:")
    assert LEAK not in r.text and "SECRET" not in r.text
    doctor.invalidate()


# ---------------------------------------------------------------- Codex: credential_store()

class FakeAgent:
    def __init__(self, exe="/fake/bin/codex", device_auth=True):
        self._exe, self._da = exe, device_auth

    def bin(self):
        return self._exe

    def device_auth(self, fetch=True):
        return self._da

    def warm_login_caps(self):
        pass


@pytest.fixture
def codex_box(codex_home, monkeypatch, projects_dir):
    monkeypatch.setattr(cx, "_agent", lambda: FakeAgent())
    monkeypatch.setattr(cx, "_cred_cache", None)
    return SimpleNamespace(home=codex_home, config=codex_home / "config.toml", auth=codex_home / "auth.json")


def put_config(box, text):
    box.config.write_text(text)


def test_credential_store_variants(codex_box):
    S = cx.credential_store
    assert S() == (None, False, True, None), "nothing at all: usable (a first codex login makes the file)"
    codex_box.auth.write_bytes(b'{"tokens":"SECRET-AUTH"}')
    assert S() == (None, True, True, None), "unset with an auth.json"
    put_config(codex_box, 'model = "gpt-6"\n')
    assert S() == (None, True, True, None), "a config.toml without the key"
    put_config(codex_box, 'cli_auth_credentials_store = "file"\n')
    assert S() == ("file", True, True, None)
    codex_box.auth.unlink()
    assert S() == ("file", False, True, None), "file before the first login: usable"
    put_config(codex_box, 'cli_auth_credentials_store = "keyring"\n')
    assert S() == ("keyring", False, False, cx.REASON_KEYRING)
    codex_box.auth.write_bytes(b"{}")
    assert S() == ("keyring", True, False, cx.REASON_KEYRING), "a leftover auth.json does not make the keyring a file"
    codex_box.auth.unlink()
    put_config(codex_box, 'cli_auth_credentials_store = "ephemeral"\n')
    assert S() == ("ephemeral", False, False, cx.REASON_EPHEMERAL)
    put_config(codex_box, 'cli_auth_credentials_store = "auto"\n')
    assert S() == ("auto", False, False, cx.REASON_AUTO), "auto with no file: refused, softened"
    codex_box.auth.write_bytes(b"{}")
    assert S() == ("auto", True, True, None), "auto with an auth.json: the file is there to swap"


def test_credential_store_treats_a_broken_config_as_unset_and_never_raises(codex_box):
    for text in ("this is [not toml", 'cli_auth_credentials_store = 5\n', 'cli_auth_credentials_store = "weird"\n', "\x00\x01", '[a]\nb = '):
        put_config(codex_box, text)
        assert cx.credential_store() == (None, False, True, None), repr(text)
    put_config(codex_box, 'cli_auth_credentials_store = " KeyRing "\n')
    assert cx.credential_store().setting == "keyring", "case and spaces do not matter"
    codex_box.config.unlink()
    codex_box.config.mkdir()                                              # a directory where the file should be
    assert cx.credential_store() == (None, False, True, None)
    codex_box.config.rmdir()
    put_config(codex_box, 'cli_auth_credentials_store = "keyring"\n' + "# x\n" * (cx.CONFIG_MAX // 4 + 10))
    assert cx.credential_store().setting is None, "a config.toml over the size cap is not parsed"


def test_credential_store_never_opens_auth_json(codex_box, monkeypatch):
    codex_box.auth.write_bytes(b'{"tokens":"SECRET-AUTH"}')
    put_config(codex_box, 'cli_auth_credentials_store = "file"\n')
    opened = []
    for name in ("read_bytes", "read_text", "open"):
        orig = getattr(Path, name)
        monkeypatch.setattr(Path, name, lambda self, *a, _o=orig, **k: (opened.append(self.name), _o(self, *a, **k))[1])
    import builtins
    real_open = builtins.open
    monkeypatch.setattr(builtins, "open", lambda f, *a, **k: (opened.append(os.path.basename(str(f))), real_open(f, *a, **k))[1])
    cx.credential_store()
    cx.supported()
    cx.store_view(0)
    assert "auth.json" not in opened and "config.toml" in opened


def test_the_codex_store_is_read_again_when_config_toml_changes(codex_box):
    put_config(codex_box, 'cli_auth_credentials_store = "file"\n')
    assert cx.credential_store().setting == "file"
    put_config(codex_box, 'cli_auth_credentials_store = "keyring"  # now\n')
    assert cx.credential_store().setting == "keyring"


def test_supported_follows_the_store(codex_box, monkeypatch):
    assert cx.supported() is True and cx.support_reason() is None
    put_config(codex_box, 'cli_auth_credentials_store = "keyring"\n')
    assert cx.supported() is False and cx.support_reason() == cx.REASON_KEYRING == KEYRING_TEXT
    put_config(codex_box, 'cli_auth_credentials_store = "file"\n')
    assert cx.supported() is True
    monkeypatch.setattr(cx, "_agent", lambda: FakeAgent(exe=None))
    assert cx.supported() is False and cx.support_reason() == cx.REASON_NOT_INSTALLED == "codex is not installed"


def test_the_reasons_have_no_colon_space_so_the_page_shows_them_whole(codex_box):
    for r in (cx.REASON_KEYRING, cx.REASON_EPHEMERAL, cx.REASON_AUTO):
        assert ": " not in r and "—" not in r and r.endswith("to use saved logins")


def test_store_view_and_the_mutating_calls_refuse_with_the_keyring_text(codex_box, home):
    put_config(codex_box, 'cli_auth_credentials_store = "keyring"\n')
    assert cx.store_view(0) == {"supported": False, "add": False, "reason": cx.REASON_KEYRING, "count": 0}
    db = DB(settings.db_path)
    for call in (lambda: cx.start_login(db, "Work"), lambda: cx.logout(db), lambda: cx.forget(db, "a" * 24)):
        with pytest.raises(cx.Unsupported) as e:
            call()
        assert str(e.value) == KEYRING_TEXT
    assert cx.finalize(db) is None and cx.tick(db) is None
    put_config(codex_box, 'cli_auth_credentials_store = "file"\n')
    assert cx.store_view(0) == {"supported": True, "add": True, "reason": None, "count": 0}


def test_the_codex_api_answers_409_with_the_keyring_text(codex_box, lite_client, home):
    put_config(codex_box, 'cli_auth_credentials_store = "keyring"\n')
    r = lite_client.get("/api/codex-accounts", headers=H).json()
    assert r["store"] == {"supported": False, "add": False, "reason": KEYRING_TEXT, "count": 0}
    r = lite_client.post("/api/codex-accounts/login", headers=H, json={"label": "Work"})
    assert r.status_code == 409 and r.json()["detail"] == KEYRING_TEXT
    st = lite_client.get("/api/state", headers=H).json()
    assert st["codex_accounts"]["store"]["reason"] == KEYRING_TEXT


# ---------------------------------------------------------------- doctor: codex-cred-store (every system)

@pytest.mark.parametrize("system", ["linux", "macos", "windows"])
def test_doctor_codex_cred_store_per_store_on_every_system(codex_box, monkeypatch, system):
    set_system(monkeypatch, system)
    D = doctor._c_codex_cred_store
    out = D(None)
    assert out.status == "pass" and "not set" in out.detail and "(to verify)" in out.detail, out
    codex_box.auth.write_bytes(b"SECRET-AUTH")
    out = D(None)
    assert out.status == "pass" and "auth.json exists" in out.detail and "saved Codex logins can be switched" in out.detail
    put_config(codex_box, 'cli_auth_credentials_store = "file"\n')
    assert D(None).status == "pass" and "is file" in D(None).detail
    put_config(codex_box, 'cli_auth_credentials_store = "keyring"\n')
    out = D(None)
    assert out.status == "warn" and "keyring" in out.detail and 'cli_auth_credentials_store = "file"' in out.fix["text"]
    put_config(codex_box, 'cli_auth_credentials_store = "ephemeral"\n')
    assert D(None).status == "warn" and "not keep its login on disk" in D(None).detail
    codex_box.auth.unlink()
    put_config(codex_box, 'cli_auth_credentials_store = "auto"\n')
    out = D(None)
    assert out.status == "warn" and "may be in the keyring" in out.detail
    codex_box.auth.write_bytes(b"SECRET-AUTH")
    assert D(None).status == "pass" and "is auto" in D(None).detail
    monkeypatch.setattr(cx, "_agent", lambda: FakeAgent(exe=None))
    assert D(None).status == "skip" and "not installed" in D(None).detail
    for o in (out, D(None)):
        assert "SECRET" not in json.dumps([o.detail, o.fix])


def test_the_codex_store_check_is_registered_in_the_codex_group():
    reg = {c[0]: c for c in doctor.CHECKS}
    assert reg["codex-cred-store"][1] == "codex" and reg["codex-cred-store"][3] is doctor._c_codex_cred_store
