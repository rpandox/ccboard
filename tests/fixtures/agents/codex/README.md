# Codex pane and hook fixtures

`folder_trust.txt` is reconstructed from issue #330's quoted Codex 0.161.0 screen, not a captured terminal. `command_approval.txt` combines issue #331's command/reason with the existing `approval_dialog.txt` menu; the file and commit message are stand-ins.

`permission_exec.json` is synthetic, built from Codex `rust-v0.161.0`: `codex-rs/core/src/tools/approvals.rs` maps `ExecCommand` to `PermissionRequestPayload::bash`; `tools/sandboxing.rs` supplies `command` and optional `description`; `hook_runtime.rs` supplies the session and working directory. This verifies board intake, not whether the hook actually ran during the box incident. Existing captured lifecycle hooks live in `tests/fixtures/codex_0161/`; none captures that incident's command approval.
