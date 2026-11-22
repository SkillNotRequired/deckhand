#!/usr/bin/env python3
"""Reminders at a set time and date, for Deckhand.

Each reminder is a persistent systemd user timer (frame-reminder-<id>.timer)
that starts timer_overlay at the chosen time; it shows the reminder at the
top-centre of your view and sounds the alarm. Persistent=true: if the Frame was
off at that time, it shows right after the Frame next starts. Nothing runs
until the timer fires, so a waiting reminder costs no CPU or battery.

The reminder's text lives only in its data folder (meta.json and the rendered
picture), never in the unit files: a line break or a "%" in it can't change them.

    reminders.py --done <id>    (run by the reminder itself afterwards: cleans up)
    reminders.py --list
"""
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time

import paths

BIN = os.environ.get("FRAME_REMINDER_BIN") or paths.tool("timer_overlay")  # override: tests only
DATA = os.path.expanduser("~/.local/share/frame-settings/reminders")
UNIT_DIR = os.path.expanduser("~/.config/systemd/user")
_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z_-]{0,63}")  # ids name files and units: nothing else allowed


def _systemctl(*args):
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(args, 1, "", str(e))


def _valid(rid):
    return isinstance(rid, str) and _ID.fullmatch(rid) is not None


def _unit(rid, kind):
    return f"frame-reminder-{rid}.{kind}"


def _write(path, text):
    """Writes a file whole or not at all (a hidden temp name, then renamed into place)."""
    tmp = os.path.join(os.path.dirname(path), f".{os.path.basename(path)}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def image_path(rid):
    """Where the app renders the reminder picture before calling add()."""
    if not _valid(rid):
        raise ValueError(f"bad reminder id: {rid!r}")
    d = os.path.join(DATA, rid)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "done.rgba")


def new_id(when):
    return time.strftime("%Y%m%d-%H%M", time.localtime(when)) + f"-{int(time.time() * 1000) % 100000:05d}"


def unit_texts(rid, when):
    """(service, timer) unit files of reminder `rid` at epoch `when`."""
    d = os.path.join(DATA, rid)
    service = (f"[Unit]\nDescription=Deckhand reminder {rid}\n\n[Service]\nType=simple\n"
               f"ExecStart={BIN} {when:.0f} {d} 0\n"
               f"ExecStopPost=/usr/bin/python3 {os.path.abspath(__file__)} --done {rid}\n")
    # UTC: a local time is ambiguous in the hour that repeats when the clocks go back
    cal = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(when))
    timer = (f"[Unit]\nDescription=Deckhand reminder {rid}\n\n"
             f"[Timer]\nOnCalendar={cal}\nPersistent=true\nAccuracySec=1s\n\n"
             "[Install]\nWantedBy=timers.target\n")
    return service, timer


def add(rid, when, text):
    """Schedules reminder `rid` (picture already rendered) for epoch `when`. Returns (ok, msg)."""
    if not _valid(rid):
        return False, "bad reminder id"
    if when <= time.time():
        return False, "that time has already passed"
    if not os.access(BIN, os.X_OK):
        return False, "timer_overlay is missing: run helpers/build.sh on the Frame"
    d = os.path.join(DATA, rid)
    service, timer = unit_texts(rid, when)
    try:
        os.makedirs(d, exist_ok=True)
        _write(os.path.join(d, "meta.json"),
               json.dumps({"id": rid, "when": when, "text": text, "created": time.time()}))
        os.makedirs(UNIT_DIR, exist_ok=True)
        _write(os.path.join(UNIT_DIR, _unit(rid, "service")), service)
        _write(os.path.join(UNIT_DIR, _unit(rid, "timer")), timer)
    except OSError as e:
        delete(rid)
        return False, f"couldn't save it ({e.strerror or e})"
    _systemctl("daemon-reload")
    r = _systemctl("enable", "--now", _unit(rid, "timer"))
    if r.returncode != 0:
        delete(rid)
        return False, (r.stdout + r.stderr).strip()[:200] or "couldn't schedule it"
    return True, "scheduled"


def delete(rid, from_service=False):
    """Removes a reminder. from_service: called by the reminder's own stop step, so it must
    not stop (wait on) itself."""
    if not _valid(rid):
        return  # never a path outside the reminders folder
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
    """Scheduled reminders, soonest first: [{'id', 'when', 'text', ...}]. Only reads files."""
    out = []
    try:
        names = os.listdir(DATA)
    except OSError:
        return out
    for rid in names:
        if not _valid(rid):
            continue
        try:
            with open(os.path.join(DATA, rid, "meta.json"), encoding="utf-8") as f:
                m = json.load(f)
            when = float(m["when"])
        except (OSError, ValueError, KeyError, TypeError):
            continue  # being written or deleted, or damaged
        if math.isfinite(when):
            out.append({**m, "id": rid, "when": when, "text": str(m.get("text", ""))})
    return sorted(out, key=lambda m: m["when"])


if __name__ == "__main__":
    if sys.argv[1:2] == ["--done"] and len(sys.argv) == 3:
        delete(sys.argv[2], from_service=True)
    elif sys.argv[1:] == ["--list"]:
        print(json.dumps(upcoming(), indent=2))
    else:
        print(__doc__)
