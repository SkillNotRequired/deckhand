#!/bin/bash
# Starts Deckhand. Used by the Steam library shortcut.
# Clears Steam's library overrides so the system Python/GTK load cleanly,
# and keeps the last run's output in last-run.log for troubleshooting.
HERE=$(cd "$(dirname "$0")" && pwd)
unset LD_PRELOAD LD_LIBRARY_PATH PYTHONHOME PYTHONPATH
export GDK_BACKEND=x11
mkdir -p "$HERE/logs"
exec /usr/bin/python3 "$HERE/app/deckhand.py" "$@" >"$HERE/logs/last-run.log" 2>&1
