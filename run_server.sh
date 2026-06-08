#!/bin/sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"

if [ -f "$SCRIPT_DIR/.env.local" ]; then
  set -a
  . "$SCRIPT_DIR/.env.local"
  set +a
fi

exec python3 "$SCRIPT_DIR/server.py"
