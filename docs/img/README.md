# Screenshots

For people who edit the project README: what its two screenshots show and how to retake them.

[Back to the documentation index](../README.md)

The screenshots live here, in `docs/img/`, and not under `app/static/`, because the board's service worker precaches every file under `app/static/`. Each one is taken in demo mode (`?demo=1`, made-up names only), is 300 KB or less, and has alt text where the README shows it. `tests/test_docs.py` checks that both files exist and that the README shows exactly these two.

| File | What it shows |
|---|---|
| `home-1280.png` | The Home page at 1280 by 800 pixels: the navigation and the project list on the left, the summary line with a count per state, the nodes strip, and the "Needs you" inbox with a permission request and its Allow, Deny and reply controls. |
| `terminal-390.png` | The terminal page on a phone, 390 pixels wide: the session header with its state, the terminal output, the key strip (Esc, Tab, arrows, Ctrl-C, Enter) and the quick-reply chips above the send box. The terminal text is made up by the development stand-in for ttyd. |

## Retaking them

1. Start a development board as described in [Developing ccboard](../development.md#demo-mode) (the temporary directories, the stand-in agents and `CCBOARD_DEV_BYPASS_USER`, which also serves the fake terminal at `/tty/`).
2. Open `http://127.0.0.1:8000/?demo=1` at a window 1280 pixels wide for the Home page. Open `/term/<session>?demo=1` at 390 pixels wide for the terminal page, where `<session>` is the tmux name of a demo session.
3. Save each as a PNG under the same name, keep it at 300 KB or less, and look at it yourself: nothing may show a real name, path or email.
