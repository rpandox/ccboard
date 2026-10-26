"""Final environment scrub after tmux's login shell has sourced its rc files."""
import os
import sys
from pathlib import Path
from profile_env import SELECTED, scrub, setup_token_value


def main():
    env = scrub(os.environ)
    env.update({k: os.environ[k] for k in SELECTED if k in os.environ})
    if env.get("CLAUDE_CONFIG_DIR"):
        token = Path(env["CLAUDE_CONFIG_DIR"]) / ".setup-token"
        try:
            value = setup_token_value(token)
        except (OSError, ValueError):
            sys.exit(f"{token}: cannot use setup token (requires nonempty UTF-8, user ownership and mode 0600)")
        if value is not None:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = value
    os.execvpe(sys.argv[1], sys.argv[1:], env)


if __name__ == "__main__":
    main()
