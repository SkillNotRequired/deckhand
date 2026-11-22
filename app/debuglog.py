"""Deckhand's debug log: one timeline of what the app did and what the cameras and SteamVR did.

Written by the app (every tap, switch, page, program it starts, message and error) and by the
camera watch service (camwatch.py: camera faults, colour camera drop-outs, headset on/off, SteamVR
stops, starts and crashes, apps Steam starts, once-a-minute temperatures). One line per event:

    2026-11-22 15:02:18.334  app      tap          House › Start walking
    2026-11-22 15:03:01.890  camera   FAULT        camera processor fault (ICP) … | headset on | …

File: <install folder>/logs/debug.log (rotated at 4 MB: debug.log.1 … .3 are older). On by
default; System › Debug log switches it (setting: ~/.config/frame-settings/debug.json).
"""
import json
import os
import threading
import time

import paths

PATH = os.path.join(paths.LOGS, "debug.log")
CONF = os.path.expanduser("~/.config/frame-settings/debug.json")
STATUS = os.path.join(paths.runtime_dir(), "camwatch.json")  # written by camwatch.py, read by the app
MAX_BYTES = 4 * 1024 * 1024
KEEP = 3
_lock = threading.Lock()
_enabled = None


def enabled():
    """Is the debug log on (default: yes)?"""
    global _enabled
    if _enabled is None:
        try:
            with open(CONF, encoding="utf-8") as f:
                _enabled = bool(json.load(f).get("enabled", True))
        except (OSError, ValueError, AttributeError):
            _enabled = True
    return _enabled


def set_enabled(on):
    global _enabled
    _enabled = bool(on)
    os.makedirs(os.path.dirname(CONF), exist_ok=True)
    tmp = CONF + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"enabled": _enabled}, f)
    os.replace(tmp, CONF)


def stamp(t=None):
    t = time.time() if t is None else t
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + ".%03d" % int(t % 1 * 1000)


def log(source, event, detail="", t=None, force=False):
    """Appends one line. Several processes write this file, so each line goes out in one write
    (O_APPEND): lines never mix. force=True logs even while the log is switched off."""
    if not (force or enabled()):
        return
    detail = " ".join(str(detail).split())  # always one line
    line = f"{stamp(t)}  {source:<8} {event:<12} {detail}".rstrip()[:2000] + "\n"
    with _lock:
        try:
            os.makedirs(paths.LOGS, exist_ok=True)
            fd = os.open(PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                os.write(fd, line.encode("utf-8", "replace"))
            finally:
                os.close(fd)
        except OSError:
            pass


def rotate(max_bytes=MAX_BYTES):
    """Starts a new file once the log is over max_bytes (keeps KEEP older ones)."""
    try:
        if os.path.getsize(PATH) < max_bytes:
            return False
    except OSError:
        return False
    with _lock:
        for i in range(KEEP - 1, 0, -1):
            if os.path.exists(f"{PATH}.{i}"):
                os.replace(f"{PATH}.{i}", f"{PATH}.{i + 1}")
        os.replace(PATH, PATH + ".1")
    return True


def tail(lines=300, path=None):
    """The last `lines` lines (reads only the end of the file)."""
    path = path or PATH
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            pos, data = f.tell(), b""
            while pos > 0 and data.count(b"\n") <= lines:
                step = min(65536, pos)
                pos -= step
                f.seek(pos)
                data = f.read(step) + data
    except OSError:
        return []
    return data.decode("utf-8", "replace").splitlines()[-lines:]


def clear():
    with _lock:
        for p in [PATH] + [f"{PATH}.{i}" for i in range(1, KEEP + 1)]:
            try:
                os.remove(p)
            except OSError:
                pass


def boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def status():
    """The camera watch's figures for the current boot (see camwatch.py), or {}."""
    try:
        with open(STATUS, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        return {}
    return st if isinstance(st, dict) and st.get("boot") == boot_id() else {}


def report(lines=1500, header=()):
    """A shareable text: what this is, the camera figures for this boot, then the newest log lines."""
    st = status()
    out = [f"Deckhand debug report, {stamp()[:19]}", *header]
    try:
        with open("/proc/uptime", encoding="utf-8") as f:
            up = float(f.read().split()[0])
        out.append(f"Frame running for {int(up // 3600)} h {int(up % 3600 // 60)} min "
                   f"(since {time.strftime('%H:%M', time.localtime(time.time() - up))})")
    except (OSError, ValueError):
        pass
    if st:
        out.append(f"This boot: {st.get('faults', 0)} camera processor faults (stuck), {st.get('recoveries', 0)} "
                   f"camera processor resets (recovered), {st.get('smmu', 0)} memory-unit faults, "
                   f"{st.get('colour_cam', 0)} colour camera drop-outs, {st.get('crashes', 0)} crashes, "
                   f"{st.get('wakes', 0)} times put on")
        if st.get("last_fault"):
            out.append(f"Last fault: {stamp(st['last_fault'])[:19]}: {st.get('last_fault_text', '')}")
    else:
        out.append("Camera watch: no figures (it isn't running, or hasn't started yet)")
    out += ["", f"---- last {lines} log lines ({PATH}) ----"]
    out += tail(lines)
    return "\n".join(out) + "\n"
