#!/bin/sh
# Start the Codex Context Editor and open it in the browser.
cd "$(dirname "$0")"
PORT="${CTX_EDITOR_PORT:-7317}"
( sleep 1; open "http://127.0.0.1:$PORT/${1:+?thread=$1}" ) &
exec python3 server.py

