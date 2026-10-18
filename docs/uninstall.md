# Uninstall ccboard

For people removing ccboard from a Linux machine.

[Back to the documentation index](README.md)

### Uninstall

```sh
sudo systemctl disable --now ccboard ccboard-ttyd ccboard-tmux code-server@$USER
sudo systemctl disable --now ccboard-mem        # only when you enabled CCBOARD_MEM_SERVICE; the plugin itself stays (claude plugin uninstall claude-mem@thedotmack)
python3 ~/.local/share/ccboard/app/scripts/codex_hooks.py remove   # or scripts/codex_hooks.py from the checkout; then: codex mcp remove ccboard
sudo rm /etc/systemd/system/ccboard*.service /etc/ccboard/env; sudo rm -r /etc/systemd/system/code-server@$USER.service.d
sudo tailscale serve --https=443 --set-path=/tty off      # use your CCBOARD_HTTPS_PORT
sudo tailscale serve --https=443 --set-path=/ off
sudo tailscale serve --https=8443 --set-path=/ off        # use your CODE_HTTPS_PORT
```
In docker mode also run `docker compose -f ~/.local/share/ccboard/compose/docker-compose.yml --profile prod down` first (and `docker image rm ghcr.io/rpandox/ccboard` if you want the image gone).

Projects in `PROJECTS_DIR`, `~/.claude` and `~/.codex` are left alone.
