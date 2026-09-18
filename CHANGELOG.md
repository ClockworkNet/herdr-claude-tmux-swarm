# Changelog

## 0.2.0

### Added

A swarm view now gets resized by 0.02 of its split and straight back, right after the watcher
opens it. The watcher logs the pair as `nudged <pane> right/left by 0.02 to settle the draw`.

A pane that tmux attaches to at the moment of the split can come up drawn at the wrong size, and
resizing it by hand is what fixes it. The nudge does that automatically. The two halves are equal
and opposite, so the split ratio ends where it started, verified at `0.50000` before and after.
If either resize fails the watcher logs it and leaves the pane alone, since a badly drawn view
beats one left at the wrong size.

### What this does not fix

The nudge is not a reliable fix and the size problem is a race, so it is worth saying what was
measured rather than what was hoped.

Opening the same view four times produced three different client sizes against a pane whose
rect was 96x64: `94x62` (correct), `192x64` (the full tab width), and `30x28` twice. Nudging a
pane that had been stuck at `30x28` for 25 seconds left it at `30x28`. A nudge applied to a
settled pane does move the pty, measured as `30x28` to `28x28` and back, so the mechanism works.
What it cannot do is escape the wrong base size the pane was given when it was created.

Both observed sizes match a client exactly. The 192x64 workspace area halves to `94x62` after
borders, which is the correct outcome. A second terminal attached to the same Herdr session at
63x32 halves to roughly `30x28`, which is the wrong one. That points at which client's geometry
the new split inherits, rather than at anything tmux or the attach is doing.

## 0.1.0

First release. The watcher polls the tmux socket directory every two seconds, maps a live swarm's
pid to the Herdr pane that owns it, and opens the swarm in a split beside it. It closes that view
when the swarm ends, and only closes panes it opened.

Pane actions for show, hide and toggle, plus watcher start, stop and status.

The watcher loop and each pane action are separate processes that read and write `swarms.json`,
so both take `state.lock` and re-read the file inside it. Neither opens a view without first
asking Herdr whether a pane is already attached to that socket.

The pane attaches with `tmux -u`. Herdr launches plugin commands with no locale in the
environment, and without that flag tmux replaces every non-ASCII glyph with a placeholder.
