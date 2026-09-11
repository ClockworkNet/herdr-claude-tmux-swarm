#!/bin/bash
# Entry wrapper for every manifest command. Herdr launches argv commands with the
# server's PATH, which on macOS can lack /opt/homebrew/bin, so resolve python3
# from known locations instead of trusting PATH.
set -u
here="$(cd "$(dirname "$0")" && pwd)"
py=""
for c in "${CW_PYTHON3:-}" /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3 "$(command -v python3 2>/dev/null)"; do
  if [ -n "$c" ] && [ -x "$c" ]; then py="$c"; break; fi
done
if [ -z "$py" ]; then
  echo "cw.claude-swarm: no python3 found" >&2
  exit 127
fi
exec "$py" "$here/swarm_watcher.py" "$@"
