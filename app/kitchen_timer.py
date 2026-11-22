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

import paths

BIN = paths.tool("timer_overlay")
IMG_DIR = os.path.expanduser("~/.cache/frame-settings/timer")
STATE = os.path.expanduser("~/.config/frame-settings/timer.json")
UNIT = "frame-kitchen-timer"
POPUP_SECONDS = 60
DONE_SHOWN_SECONDS = 30  # "Time's up" stays up this long after zero
BOOT_ID_FILE = "/proc/sys/kernel/random/boot_id"


def _systemctl(*args):
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(args, 1, "", str(e))


_boot = None


def _boot_id():
    """This boot's id (new at every start of the Frame), or None where there is none."""
    global _boot
    if _boot is None:
        try:
            with open(BOOT_ID_FILE, encoding="utf-8") as f:
                _boot = f.read().strip()
        except OSError:
            _boot = ""
    return _boot or None


def _job_exists():
    """Is the timer's job still there? systemd keeps a transient unit's file in
    $XDG_RUNTIME_DIR/systemd/transient while the job exists (--collect removes it when the job
    ends), so this is a file check, not a process launch."""
    run = os.environ.get("XDG_RUNTIME_DIR")
    folder = os.path.join(run, "systemd", "transient") if run else ""
    if not folder or not os.path.isdir(folder):
        return True  # can't tell here: go by the saved end time
    return os.path.exists(os.path.join(folder, UNIT + ".service"))


def status():
    """{'label', 'end', 'minutes'} of the current timer, or None.

    Cheap enough to call every second (no process launched). The state file says what was set;
    the timer only counts if it was set in this boot and its job still exists: a reboot, a crash
    or a shut-off ends the job, and then nothing would ring. The job ends by itself 30 s after
    zero; Cancel removes the file."""
    try:
        with open(STATE, encoding="utf-8") as f:
            st = json.load(f)
        end = float(st["end"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    boot, saved = _boot_id(), st.get("boot")
    if boot and saved and saved != boot:
        return None  # set before the Frame last started
    if time.time() > end + DONE_SHOWN_SECONDS + 5 or not _job_exists():
        return None
    st["end"] = end
    st.setdefault("label", "Timer")
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
        return False, "timer_overlay is missing: run helpers/build.sh on the Frame"
    cancel()
    _systemctl("reset-failed", UNIT)
    end = time.time() + seconds
    popup = max(0, min(POPUP_SECONDS, int(seconds)))
    name = " ".join(str(label).split())[:60] or "Timer"  # one plain line for systemd's description
    try:
        r = subprocess.run(["systemd-run", "--user", "--quiet", "--collect", f"--unit={UNIT}",
                            f"--description=Deckhand timer: {name}",
                            BIN, f"{end:.3f}", IMG_DIR, str(popup)], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)[:200]
    if r.returncode != 0:
        return False, (r.stdout + r.stderr).strip()[:200] or "systemd-run failed"
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = f"{STATE}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"label": label, "end": end, "minutes": seconds / 60, "boot": _boot_id()}, f)
    os.replace(tmp, STATE)
    return True, "started"
