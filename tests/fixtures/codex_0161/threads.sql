-- The shape of Codex 0.161's state_5.sqlite `threads` table as the box check found it: has_user_event is 0 on every row (even threads
-- with a first message and tokens), first_user_message and originator are columns of their own. Only the columns the board reads, plus
-- a few it ignores. Rows are inserted by tests/test_codex_box_fixes.py.
CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, created_at INTEGER, updated_at INTEGER, source TEXT, model_provider TEXT,
  cwd TEXT, title TEXT, name TEXT, sandbox_policy TEXT, approval_mode TEXT, tokens_used INTEGER DEFAULT 0, has_user_event INTEGER DEFAULT 0,
  archived INTEGER DEFAULT 0, git_branch TEXT, model TEXT, reasoning_effort TEXT, first_user_message TEXT DEFAULT '', originator TEXT);
CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT PRIMARY KEY, status TEXT);
