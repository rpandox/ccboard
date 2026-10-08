# ccboard container image (v0.5.1-docker). Built only by GitHub Actions and published as ghcr.io/rpandox/ccboard.
#
# The image carries the board (FastAPI, venv) and the CLIs it shells out to. It does NOT carry Claude Code or Codex:
# the container runs with the host home mounted at the same path, so the host's ~/.local/bin/{claude,codex,ccusage}
# (same libc, same uid) are the ones that run. Everything that must outlive a restart (the tmux server that owns the
# sessions, ttyd, code-server, tailscale serve) stays on the host. See deploy/docker-compose.yml.
#
# Layer order is cache order: apt, third-party repos, user, python deps, then the app and the per-build metadata.
FROM ubuntu:26.04

ARG DEBIAN_FRONTEND=noninteractive

# tmux 3.4 here is the same version as the host's: the tmux client and server must speak the same protocol.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip git tmux curl ca-certificates gnupg openssh-client iproute2 procps restic sqlite3 less \
 && rm -rf /var/lib/apt/lists/*

# GitHub CLI from the official apt repository.
RUN install -d -m 0755 /etc/apt/keyrings \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*

# Node 22 (NodeSource): the host's ccusage needs node >= 20, and the image's own ccusage (exact version pinned) is the fallback.
# ccusage 20 ships a native binary without the execute bit and chmods it on first run, which a non-root user cannot do
# to root-owned files: set the bit here, and run it once so a broken package fails the build instead of the board.
RUN install -d -m 0755 /etc/apt/keyrings \
 && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" > /etc/apt/sources.list.d/nodesource.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g --no-fund --no-audit ccusage@20.0.26 \
 && find /usr/lib/node_modules/ccusage -path '*/bin/ccusage' -type f -exec chmod 0755 {} + \
 && ccusage --version \
 && npm cache clean --force \
 && rm -rf /var/lib/apt/lists/*

# Tailscale CLI only, for `tailscale serve` (previews) through the host daemon's socket mounted from /var/run/tailscale.
# The repo follows the base image's codename (resolute for 26.04), so the next base bump needs no edit here.
RUN . /etc/os-release \
 && curl -fsSL "https://pkgs.tailscale.com/stable/ubuntu/${VERSION_CODENAME}.noarmor.gpg" -o /usr/share/keyrings/tailscale-archive-keyring.gpg \
 && curl -fsSL "https://pkgs.tailscale.com/stable/ubuntu/${VERSION_CODENAME}.tailscale-keyring.list" -o /etc/apt/sources.list.d/tailscale.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends tailscale \
 && rm -rf /var/lib/apt/lists/*

# The base image's 'ubuntu' user holds uid/gid 1000: drop it and take its place with the board user. Its home is the
# host's home mounted at runtime, so no directory is created here (-M); the passwd entry still gives the right shell
# and home to anything that looks the user up (config.py reads the login shell from it).
RUN if getent passwd 1000 >/dev/null; then userdel -r "$(getent passwd 1000 | cut -d: -f1)"; fi \
 && if getent group 1000 >/dev/null; then groupdel "$(getent group 1000 | cut -d: -f1)"; fi \
 && groupadd -g 1000 ccboard \
 && useradd -M -u 1000 -g 1000 -s /bin/bash -d /home/rpandox ccboard \
 && install -d -o 1000 -g 1000 /opt/ccboard

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    TMUX_TMPDIR=/tmp \
    CCBOARD_RUNTIME=docker \
    PATH=/opt/ccboard/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# HOME is not set here: compose sets it to the host home path. The entrypoint and compose put $HOME/.local/bin
# (the host's claude, codex, ccusage) in front of this PATH.

WORKDIR /opt/ccboard

# Python dependencies first so a code-only change reuses this layer. requirements.lock pins exact versions with hashes
# (regenerate from requirements.in, see the README): two builds of one commit install the same packages. The venv is created and chowned in one layer.
COPY --chown=1000:1000 requirements.lock ./
RUN python3 -m venv .venv \
 && .venv/bin/pip install --no-cache-dir --disable-pip-version-check --require-hashes -r requirements.lock \
 && chown -R 1000:1000 .venv

COPY --chown=1000:1000 app ./app
COPY --chown=1000:1000 bin ./bin
COPY --chown=1000:1000 scripts ./scripts
COPY --chown=1000:1000 tmux.conf ./

# Per-build metadata last: it changes on every commit and must not invalidate anything above.
ARG CCBOARD_VERSION=dev
ARG CCBOARD_REVISION=unknown
ENV CCBOARD_IMAGE_VERSION=${CCBOARD_VERSION}
# Which repository's CI built this image and at which commit: the doctor's GitHub Actions check (empty in a local build, which skips it).
ARG CCBOARD_SOURCE_REPO=
ARG CCBOARD_IMAGE_REVISION=
ENV CCBOARD_SOURCE_REPO=${CCBOARD_SOURCE_REPO} \
    CCBOARD_IMAGE_REVISION=${CCBOARD_IMAGE_REVISION}
LABEL org.opencontainers.image.title="ccboard" \
      org.opencontainers.image.description="ccboard: a phone-first board for Claude Code and Codex sessions in tmux" \
      org.opencontainers.image.source="https://github.com/rpandox/ccboard" \
      org.opencontainers.image.version="${CCBOARD_VERSION}" \
      org.opencontainers.image.revision="${CCBOARD_REVISION}" \
      org.opencontainers.image.licenses="MIT"

USER 1000:1000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fs "http://127.0.0.1:${CCBOARD_PORT:-8000}/healthz" >/dev/null || exit 1

ENTRYPOINT ["/opt/ccboard/scripts/docker-entrypoint.sh"]
