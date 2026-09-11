#!/bin/bash
# Pane entrypoint for the cw.claude-swarm plugin. Herdr launches this in the
# split pane with the server's PATH (no /opt/homebrew/bin on macOS), so the
# watcher passes the tmux binary as CW_TMUX and the socket name as CW_SWARM_SOCKET.
set -u
sock="${CW_SWARM_SOCKET:-}"
tmux_bin=""
for c in "${CW_TMUX:-}" /opt/homebrew/bin/tmux /usr/local/bin/tmux /usr/bin/tmux "$(command -v tmux 2>/dev/null)"; do
  if [ -n "$c" ] && [ -x "$c" ]; then tmux_bin="$c"; break; fi
done
# One line per launch so a pane that exits at once can still be diagnosed.
if [ -n "${HERDR_PLUGIN_STATE_DIR:-}" ] && [ -d "$HERDR_PLUGIN_STATE_DIR" ]; then
  printf '%s pane=%s cwd=%s sock=%s tmux=%s term=%s locale=%s path=%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "${HERDR_PANE_ID:-?}" "$PWD" "${sock:-unset}" "${tmux_bin:-missing}" "${TERM:-unset}" "${LC_ALL:-${LC_CTYPE:-${LANG:-unset}}}" "$PATH" >> "$HERDR_PLUGIN_STATE_DIR/pane-launch.log" 2>/dev/null
fi
if [ -z "$sock" ]; then
  echo "cw.claude-swarm: CW_SWARM_SOCKET is not set; nothing to attach to." >&2
  echo "Open this pane through the watcher or 'herdr plugin action invoke cw.claude-swarm.show'." >&2
  sleep 5; exit 1
fi
# Matches the watcher's ^claude-swarm-\d+$. The value only ever arrives from the watcher,
# which already validated it, and it is exec'd as an argv rather than re-parsed by a shell --
# but the two checks disagreeing is the kind of gap that outlives the reason for it.
case "$sock" in
  claude-swarm-|claude-swarm-*[!0-9]*) sock_ok=no ;;
  claude-swarm-[0-9]*) sock_ok=yes ;;
  *) sock_ok=no ;;
esac
if [ "$sock_ok" != yes ]; then
  echo "cw.claude-swarm: refusing odd socket name '$sock'" >&2; sleep 5; exit 1
fi
if [ -z "$tmux_bin" ]; then
  echo "cw.claude-swarm: tmux not found (PATH=$PATH)" >&2
  sleep 5; exit 127
fi
# Herdr hands the pane the server's environment, which carries no LC_ALL, LC_CTYPE
# or LANG. tmux reads the first of those that is set to decide whether the terminal
# takes UTF-8; with none set it falls back to the C locale and replaces every
# non-ASCII glyph, so box drawing and Claude's spinner arrive as placeholders. A
# shell attach looks right only because the login shell exports a UTF-8 LANG.
# `-u` forces UTF-8 output regardless; the export covers anything else the pane runs.
case "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" in
  *UTF-8*|*utf8*|*UTF8*|*utf-8*) ;;
  *)
    for loc in en_US.UTF-8 C.UTF-8; do
      if locale -a 2>/dev/null | grep -qxF "$loc"; then export LANG="$loc"; break; fi
    done
    ;;
esac
exec "$tmux_bin" -u -L "$sock" attach-session -t claude-swarm
