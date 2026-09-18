# herdr-claude-tmux-swarm

A Herdr plugin that opens a Claude Code teammate swarm in a split pane beside the Claude pane that
spawned it, and closes that pane when the swarm ends.

Claude Code with `teammateMode = "tmux"` starts a private tmux server per session at
`/tmp/tmux-<uid>/claude-swarm-<claude pid>` and tells you to attach by hand. This plugin watches
that directory and does the attach for you.

## What it does

- Polls the tmux socket directory every 2 seconds. A swarm counts as live when the socket exists,
  the owning `claude` pid is alive, and `tmux -L <socket> ls` answers.
- Finds the Herdr pane whose process tree contains that pid and opens the plugin's `swarm` pane as a
  split beside it, running `tmux -L <socket> attach-session -t claude-swarm`. Focus stays where it was.
- When the swarm's tmux server exits, the attach exits and Herdr closes the pane. The watcher also
  closes a view whose swarm died, and only panes it opened.
- Swarms already live when the watcher starts are recorded but not shown. Use the `show` action for
  those.
- If you close a view yourself, the watcher does not reopen it for that swarm.

## Requirements

- [Herdr](https://herdr.dev) 0.8.2 or newer
- Claude Code configured with `teammateMode = "tmux"`
- tmux, and python3 (stdlib only, no packages to install)
- macOS or Linux

## Install

```sh
git clone git@github.com:ClockworkNet/herdr-claude-tmux-swarm.git
herdr plugin link "$PWD/herdr-claude-tmux-swarm"
herdr plugin enable cw.claude-swarm
herdr plugin action invoke cw.claude-swarm.watcher-start
```

The plugin id is `cw.claude-swarm`; it is what actions, config and state are keyed by.

The `[[startup]]` hook starts the watcher after every Herdr session restore, so the third line is
only needed the first time or after `watcher-stop`.

## Actions

| Action | Context | Does |
| --- | --- | --- |
| `cw.claude-swarm.show` | pane | Open the view for the swarm owned by the focused pane |
| `cw.claude-swarm.hide` | pane | Close that view and stop reopening it |
| `cw.claude-swarm.toggle` | pane | Close the view if one is on screen, else open it |
| `cw.claude-swarm.watcher-start` | workspace | Start the detached watcher (single instance) |
| `cw.claude-swarm.watcher-stop` | workspace | Stop it |
| `cw.claude-swarm.status` | workspace | Print watcher pid, config, tracked swarms |

Actions run asynchronously in Herdr. `watcher-start` right after `watcher-stop` can report
`already_running` because the stop has not finished yet. Run it again.

Suggested keybinding, added to `~/.config/herdr/config.toml` by hand. `prefix+s` is Herdr's
settings key and `prefix+w` the workspace picker; `prefix+t` is unbound by default. `toggle` goes
both ways from one key, which leaves `prefix+shift+t` to rename_tab.

```toml
[[keys.command]]
key = "prefix+t"
type = "plugin_action"
command = "cw.claude-swarm.toggle"
description = "toggle Claude swarm for this pane"
```

The key works from the swarm view as well as from the pane that owns it. `show` and `hide` stay
available for a binding that only ever goes one way.

## Config

`~/.config/herdr/plugins/config/cw.claude-swarm/config.json`

```json
{ "direction": "right", "interval_seconds": 2 }
```

`direction` is `right` (default), `down`, or `auto` (right when the owner pane is at least twice as
wide as it is tall, else down). Restart the watcher after editing.

## Files and logs

- `bin/swarm_watcher.py` does all the work, stdlib only. `bin/run.sh` and `bin/swarm-pane.sh` exist
  because Herdr launches plugin commands with a PATH that lacks `/opt/homebrew/bin`, so python3 and
  tmux are resolved from known locations.
- State: `~/.local/state/herdr/plugins/cw.claude-swarm/` holds `swarms.json`, `watcher.pid`,
  `watcher.log`, and `pane-launch.log` (one line per pane launch, useful when a pane exits at once).
- The watcher loop and every pane action are separate processes that read-modify-write
  `swarms.json`, so both take `state.lock` and re-read the file inside it. Without the re-read a
  cycle holding a pre-action copy could drop the view an action had just opened, and the next
  toggle would open a second view of the same swarm. Before opening anything the code also asks
  Herdr which pane is already attached to that socket and adopts it, so state that is wrong for
  any other reason still cannot produce two copies.
- After opening a view the watcher resizes it by 0.02 of the split and back, which is what
  settling a pane drawn at the wrong size takes. The ratio ends where it started. See CHANGELOG.md
  for what this does and does not fix.
- Herdr's own view of action runs: `herdr plugin log list --plugin cw.claude-swarm`.

## Tests

```sh
python3 -m unittest discover -s tests
```

Thirty-two tests over the parts that can be tested without a running Herdr: socket-name parsing,
the liveness decision, pid-to-pane matching against a captured `herdr pane process-info` fixture,
direction choice, state file round trip, the toggle's decision, view adoption, and the state
transaction that keeps the watcher and a pane action from clobbering each other.

The live path was exercised on 2026-09-06 with a fake swarm (a `sleep` owned by a Herdr pane plus a
tmux server on a `claude-swarm-<pid>` socket): the view opened within two seconds of the socket
appearing and was gone within six seconds of `tmux kill-server`.

## CI

| Workflow | Runs | Does |
| --- | --- | --- |
| `.github/workflows/tests.yml` | push to `main`, PR | `unittest` on Python 3.9 and 3.14, Ubuntu and macOS |
| `.github/workflows/secret-scan.yml` | push to `main`, PR, manual | gitleaks over the full git history |

The secret scan checks out with `fetch-depth: 0` and runs `gitleaks git`, so a secret that was
committed and later deleted still fails the build; a shallow checkout would scan one commit and
pass without having looked. gitleaks is pinned by version and SHA256 rather than floating, so a
scan result means the same thing next month.

To run the same scan locally before pushing:

```sh
gitleaks git --redact --verbose --exit-code 1 .
```

## Contributing

Issues and pull requests are welcome. Keep `bin/swarm_watcher.py` on the standard library, and add
a test for anything that can be decided without a running Herdr.

## License

MIT. See [LICENSE](LICENSE).
