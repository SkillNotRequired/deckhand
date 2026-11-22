"""Kitchen timer for Deckhand.

The timer runs as a background systemd user job (frame-kitchen-timer), so it
keeps going with the app closed. The job is the native helper timer_overlay:
it sleeps, then shows a countdown pinned to the top-centre of your view for the
last minute, and sounds an alarm at zero. Deckhand renders the countdown
pictures (NN.rgba = seconds left, done.rgba) into IMG_DIR before starting it.
"""
import json
import os
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(HERE, "timer_overlay")
IMG_DIR = os.path.expanduser("~/.cache/frame-settings/timer")
STATE = os.path.expanduser("~/.config/frame-settings/timer.json")
UNIT = "frame-kitchen-timer"
POPUP_SECONDS = 60
DONE_SHOWN_SECONDS = 30  # "Time's up" stays up this long after zero


def _systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def running():
    return _systemctl("is-active", "--quiet", UNIT).returncode == 0


def status():
    """{'label', 'end', 'minutes'} of the current timer, or None.

    Read from the state file only (no process launched), so the app can call it
    every second cheaply. Cancel removes the file; the job ends by itself 30 s
    after zero."""
    try:
        with open(STATE, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        return None
    if time.time() > st.get("end", 0) + DONE_SHOWN_SECONDS + 5:
        return None
    return st


def cancel():
    _systemctl("stop", UNIT)
    try:
        os.remove(STATE)
    except OSError:
        pass


def start(seconds, label):
    """Starts the background job. Pictures must already be in IMG_DIR. Returns (ok, message)."""
    if not os.access(BIN, os.X_OK):
        return False, "timer_overlay is missing: run build.sh on the Frame"
    cancel()
    _systemctl("reset-failed", UNIT)
    end = time.time() + seconds
    popup = min(POPUP_SECONDS, int(seconds))
    r = subprocess.run(["systemd-run", "--user", "--quiet", "--collect", f"--unit={UNIT}",
                        f"--description=Deckhand timer: {label}",
                        BIN, f"{end:.3f}", IMG_DIR, str(popup)], capture_output=True, text=True)
    if r.returncode != 0:
        return False, (r.stdout + r.stderr).strip()[:200] or "systemd-run failed"
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump({"label": label, "end": end, "minutes": seconds / 60}, f)
    return True, "started"
