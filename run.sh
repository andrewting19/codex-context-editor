#!/bin/sh
# Start the Codex Context Editor. Usage: ./run.sh [thread-id-or-link] [--port N] [--no-browser]
cd "$(dirname "$0")" && exec python3 server.py "$@"

