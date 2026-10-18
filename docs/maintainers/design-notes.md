# Design notes

For maintainers: where the build departs from the original spec.

[Back to the documentation index](../README.md)

## Deviations from the original spec (v0.1)

Kept deliberately small; each is the simplest safe option.

1. ttyd runs a validating wrapper (`bin/ccboard-attach`, delegating device tickets to `bin/ccboard-attach-bare`) instead of `-a tmux attach-session -t`: with raw URL args a client could append `;`-separated tmux commands, i.e. get a shell.
2. ttyd is the upstream 1.7.7 static binary on both LTS releases: Ubuntu 22.04's apt ttyd (1.6.3) has no `-W`, and both apt packages ship a root `ttyd.service` on 7681.
3. Units are `ccboard-tmux`, `ccboard-ttyd`, `ccboard` (not `ttyd`): a dedicated tmux server unit keeps sessions alive across board restarts, and the name avoids the apt unit.
4. Two HTTPS ports with the terminal under `/tty` on the dashboard port (defaults 443 and 8443) instead of 443/8443/10000: same origin for the board and the terminal, one port fewer to reserve.
5. Session names are `<project>--<repo>--<session>`: the project/repo model (a project folder holding one or more repos) was added during the build.
6. Sessions start as your login shell and the launcher command is typed in, instead of `cmd; exec $SHELL`: this gives Claude your full login PATH (nvm, cargo, docker, …); the finished run still leaves a prompt.
