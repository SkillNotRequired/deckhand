#!/usr/bin/env python3
"""Reminders at a set time and date, for Deckhand.

Each reminder is a persistent systemd user timer (frame-reminder-<id>.timer)
that starts timer_overlay at the chosen time; it shows the reminder at the
top-centre of your view and sounds the alarm. Persistent=true: if the Frame was
off at that time, it shows right after the Frame next starts. Nothing runs
until the timer fires, so a waiting reminder costs no CPU or battery.

    reminders.py --done <id>    (run by the reminder itself afterwards: cleans up)
    reminders.py --list
"""
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
import paths  # noqa: E402
BIN = os.environ.get("FRAME_REMINDER_BIN") or paths.tool("timer_overlay")  # override: tests only
DATA = os.path.expanduser("~/.local/share/frame-settings/reminders")
UNIT_DIR = os.path.expanduser("~/.config/systemd/user")


def _systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def _unit(rid, kind):
    return f"frame-reminder-{rid}.{kind}"


def image_path(rid):
    """Where the app renders the reminder picture before calling add()."""
    d = os.path.join(DATA, rid)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "done.rgba")


def new_id(when):
    return time.strftime("%Y%m%d-%H%M", time.localtime(when)) + f"-{int(time.time() * 1000) % 100000:05d}"


def add(rid, when, text):
    """Schedules reminder `rid` (picture already rendered) for epoch `when`. Returns (ok, msg)."""
    if when <= time.time():
        return False, "that time has already passed"
    if not os.access(BIN, os.X_OK):
        return False, "timer_overlay is missing: run helpers/build.sh on the Frame"
    d = os.path.join(DATA, rid)
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"id": rid, "when": when, "text": text, "created": time.time()}, f)
    os.makedirs(UNIT_DIR, exist_ok=True)
    cal = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(when))
    with open(os.path.join(UNIT_DIR, _unit(rid, "service")), "w", encoding="utf-8") as f:
        f.write(f"[Unit]\nDescription=Deckhand reminder: {text}\n\n[Service]\nType=simple\n"
                f"ExecStart={BIN} {when:.0f} {d} 0\n"
                f"ExecStopPost=/usr/bin/python3 {os.path.abspath(__file__)} --done {rid}\n")
    with open(os.path.join(UNIT_DIR, _unit(rid, "timer")), "w", encoding="utf-8") as f:
        f.write(f"[Unit]\nDescription=Deckhand reminder at {cal}: {text}\n\n"
                f"[Timer]\nOnCalendar={cal}\nPersistent=true\nAccuracySec=1s\n\n"
                "[Install]\nWantedBy=timers.target\n")
    _systemctl("daemon-reload")
    r = _systemctl("enable", "--now", _unit(rid, "timer"))
    if r.returncode != 0:
        delete(rid)
        return False, (r.stdout + r.stderr).strip()[:200] or "couldn't schedule it"
    return True, "scheduled"


def delete(rid, from_service=False):
    """Removes a reminder. from_service: called by the reminder's own stop step, so it must
    not stop (wait on) itself."""
    if from_service:
        _systemctl("--no-block", "disable", "--now", _unit(rid, "timer"))
    else:
        _systemctl("disable", "--now", _unit(rid, "timer"))
        _systemctl("stop", _unit(rid, "service"))
    for kind in ("timer", "service"):
        try:
            os.remove(os.path.join(UNIT_DIR, _unit(rid, kind)))
        except OSError:
            pass
    _systemctl("daemon-reload")
    shutil.rmtree(os.path.join(DATA, rid), ignore_errors=True)


def upcoming():
    """Scheduled reminders, soonest first: [{'id', 'when', 'text'}]."""
    out = []
    try:
        names = os.listdir(DATA)
    except OSError:
        return out
    for rid in names:
        try:
            with open(os.path.join(DATA, rid, "meta.json"), encoding="utf-8") as f:
                out.append(json.load(f))
        except (OSError, ValueError):
            pass
    return sorted(out, key=lambda m: m["when"])


if __name__ == "__main__":
    if sys.argv[1:2] == ["--done"] and len(sys.argv) == 3:
        delete(sys.argv[2], from_service=True)
    elif sys.argv[1:] == ["--list"]:
        print(json.dumps(upcoming(), indent=2))
    else:
        print(__doc__)
