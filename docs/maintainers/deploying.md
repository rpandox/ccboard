# Deploying

For the maintainer of a public deployment: the person who ships the container image and runs a board that other people rely on.

[Back to the documentation index](../README.md)

Ordinary users do not need this page. If you installed ccboard to use it, from a clone or with `CCBOARD_RUNTIME=docker`, read [Upgrade](../upgrade.md) and [Uninstall](../uninstall.md) instead. This page describes the maintainer's own loop: a merge to `main` publishes the image, Watchtower swaps the container, and what to do when GitHub Actions is down. It does not change how a board behaves.

## Updating and the outage per push

**Updating.** Watchtower runs as `ccboard-watchtower` with `--interval 300 --cleanup --label-enable --scope ccboard --include-restarting`. It only touches containers labelled `com.centurylinklabs.watchtower.enable=true` **and** `com.centurylinklabs.watchtower.scope=ccboard`, which only the ccboard container carries, so the other containers on the box are never updated, restarted or removed. `scripts/deploy.sh <host>` detects docker mode on the box (a running container named `ccboard`), pushes, waits for `ghcr.io/rpandox/ccboard:sha-<7>`, pulls and recreates the container instead of waiting for Watchtower, and keeps the checkout pulled so the rollback stays current. What a host owner does after an update, and after editing the settings file, is in [Upgrade](../upgrade.md#updating-the-container).

**The outage per push.** Each push to `main` swaps the container once, and the board is unreachable for that long. What happens, in order: Watchtower finds and pulls the new image first (the board keeps running; pre-pulling would save nothing), runs the deploy gate inside the old container (`scripts/ccboard-deploy-gate`: exit 75 holds the swap while someone is at a terminal, a permission is pending or a clone runs, for at most 30 minutes, and Watchtower asks again every 5 minutes), sends SIGTERM (the compose `stop_grace_period: 6s` is the limit; it overrides Watchtower's own 30 s default), removes the old container, creates and starts the new one, and the entrypoint syncs the host-run scripts and starts the board. Measured on the box: about 10 s on an idle box (stop and remove 3 s, create and start 2 s, entrypoint 4 s, app start 0.4 s), of which 3.4 s were the host steps (hooks merge, tmux.conf, `claude mcp get`, Codex) that now run beside the board instead of before it; under heavy disk pressure docker's own remove, create and start took 100 s once, which the board cannot shorten. To measure a swap, run `scripts/swap-probe.sh 300` on the box in a second shell just before a push you were going to make anyway: it prints one line per half second and the longest gap between two `/healthz` 200 answers; join it with `docker events --since 10m --filter container=ccboard` and `docker logs ccboard-watchtower --since 10m`. Changes to `deploy/` or `install.sh` reach the box only when you rerun `./install.sh`; try them in the shadow compose (step B) first.

### When GitHub Actions is down

Every deploy normally waits for the image CI builds. When Actions is down or stuck, build on the box instead (docker mode only; GitHub is still used to carry the commit, `git push` is the one thing it must do):

```sh
scripts/deploy.sh --local-build <ssh-host> [remote-dir]
```

What it does, in order: pushes `main`; pulls the checkout on the box and refuses to go on if the box is not at the pushed commit; asks the deploy gate (`docker exec ccboard /opt/ccboard/scripts/ccboard-deploy-gate`) and **stops with a HOLD message when it answers 75** (an open terminal, a pending permission or a running clone), unless you add `--force`; builds `ccboard-local:<sha>` from the checkout with `CCBOARD_VERSION=local-<sha>` and `CCBOARD_REVISION=<full sha>` under `nice` and `ionice`, printing the elapsed time; writes `docker-compose.local.yml` next to the compose file (image `ccboard-local:<sha>`, `pull_policy: never`, and the label `com.centurylinklabs.watchtower.enable: "false"` so Watchtower never pulls `latest` over it); runs `docker compose -f docker-compose.yml -f docker-compose.local.yml --profile prod up -d ccboard`; waits for `/healthz`; and ends with `running local build <sha>`, which it also writes to `<data dir>/deploy-mode`. A failed build leaves the running container exactly as it was.

A build on a loaded box can take many minutes (nine unrelated containers share it); the board keeps running meanwhile, and Ctrl-C before the `up` swaps nothing.

**Going back.** When Actions is healthy again, run the normal `scripts/deploy.sh <ssh-host>` with no flag. It waits for the registry tag `sha-<7>`, then removes `docker-compose.local.yml`, pulls and recreates the container, and ends with `running ghcr.io/rpandox/ccboard:sha-<7>`. If that tag does not exist (CI never built the commit) the script says so and leaves the local build running with its override in place. The box does not return to the registry on its own: until you run the plain command, `<data dir>/deploy-mode` says `running local build <sha>`.

Not scripted: build somewhere else and ship the image over SSH, for example `docker buildx build --platform linux/amd64 --build-arg CCBOARD_VERSION=local-<sha> --build-arg CCBOARD_REVISION=<full sha> -t ccboard-local:<sha> --load .`, then `docker save ccboard-local:<sha> | ssh <ssh-host> docker load`, then write the same override file by hand and run the `docker compose ... up -d ccboard` line above.
