"""Shared profile environment policy; usable by the standalone post-shell exec shim."""
import os
import stat

UNSET = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "CLAUDE_CONFIG_DIR",
    "CODEX_HOME", "CODEX_SQLITE_HOME", "ANTHROPIC_PROFILE", "CLAUDE_SECURESTORAGE_CONFIG_DIR",
    "CLAUDE_CODE_ACCOUNT_UUID", "CLAUDE_CODE_USER_EMAIL", "CLAUDE_CODE_ORGANIZATION_UUID",
    "CLAUDE_CODE_PROJECT_DIR_NAME", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_CUSTOM_HEADERS", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "AWS_BEARER_TOKEN_BEDROCK",
    "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID", "OPENAI_ORGANIZATION",
)
PREFIXES = ("ANTHROPIC_FEDERATION_", "ANTHROPIC_FOUNDRY_", "ANTHROPIC_VERTEX_", "ANTHROPIC_BEDROCK_")
SELECTED = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "CODEX_SQLITE_HOME")


def scrub(env):
    return {k: v for k, v in env.items() if k not in UNSET and not k.startswith(PREFIXES)}


def setup_token_value(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if path.is_symlink():
            raise ValueError(f"{path}: must be a regular file") from None
        return None
    except OSError:
        raise ValueError(f"{path}: cannot read setup token") from None
    try:
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600:
                raise ValueError(f"{path}: must be a regular file owned by the user with mode 0600")
            value = f.read().decode("utf-8").strip()
            if not value:
                raise ValueError(f"{path}: setup token is empty")
            return value
    except UnicodeError:
        raise ValueError(f"{path}: setup token is not UTF-8") from None
    except OSError:
        raise ValueError(f"{path}: cannot read setup token") from None
