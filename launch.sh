#!/bin/bash
# Starts Deckhand. Used by the Steam library shortcut.
# Clears Steam's library overrides so the system Python/GTK load cleanly, and keeps
# the output of the last two runs for troubleshooting: logs/last-run.log (this run)
# and logs/last-run.log.1 (the one before).
HERE=$(cd "$(dirname "$0")" && pwd)
unset LD_PRELOAD LD_LIBRARY_PATH PYTHONHOME PYTHONPATH
export GDK_BACKEND=x11
LOG="$HERE/logs/last-run.log"
mkdir -p "$HERE/logs" 2>/dev/null
[ -f "$LOG" ] && mv -f "$LOG" "$LOG.1" 2>/dev/null
{ : >"$LOG"; } 2>/dev/null || LOG=/dev/null  # logging must never stop the app from starting
exec /usr/bin/python3 "$HERE/app/deckhand.py" "$@" >"$LOG" 2>&1
