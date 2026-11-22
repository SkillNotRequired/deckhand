"""Ceiling indicator for Deckhand.

Settings live in ~/.config/frame-settings/ceiling.conf (key=value) and are read by
the native overlay ceiling_overlay, run as the user service frame-ceiling.service
only while at least one mode is on:
  near      grid at the ceiling fades in when a controller or your head gets close
  always    the grid stays faintly visible
  upmarker  while Recline VR tilts the world, markers show the real ceiling and floor

The service is tied to SteamVR (PartOf/WantedBy steamvr.service): it starts with SteamVR and
is stopped before SteamVR stops or restarts, so it never reconnects to a SteamVR that is
shutting down.

measure() finds the ceiling height from a controller held against it (while not reclined).
"""
import os
import re
import struct
import subprocess
import threading
import time

import paths

CONF = os.path.expanduser("~/.config/frame-settings/ceiling.conf")
DATA = os.path.expanduser("~/.local/share/frame-settings/ceiling")
SPACE_TOOL = paths.tool("space_tool")
BIN = paths.tool("ceiling_overlay")
UNIT = "frame-ceiling.service"
UNIT_PATH = os.path.expanduser(f"~/.config/systemd/user/{UNIT}")
DEFAULTS = {"height": 2.40, "cx": 0.0, "cz": 0.0, "near": 1, "near_dist": 0.40, "always": 0,
            "always_alpha": 0.18, "upmarker": 1, "enabled": 0, "measured": 0}


def load():
    c = dict(DEFAULTS)
    try:
        with open(CONF, encoding="utf-8") as f:
            for line in f:
                if "=" not in line:
                    continue
                k, v = line.strip().split("=", 1)
                if k not in c:
                    continue
                try:
                    c[k] = float(v) if isinstance(DEFAULTS[k], float) else int(float(v))
                except ValueError:
                    pass  # one bad line keeps its default; the rest still load
    except OSError:
        pass
    return c


def save(c):
    os.makedirs(os.path.dirname(CONF), exist_ok=True)
    tmp = CONF + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for k in DEFAULTS:
            f.write(f"{k}={c.get(k, DEFAULTS[k])}\n")
    os.replace(tmp, CONF)


def any_mode(c):
    return bool(c.get("near") or c.get("always") or c.get("upmarker"))


UNIT_TEXT = f"""[Unit]
Description=Deckhand: ceiling indicator
After=steamvr.service
PartOf=steamvr.service
StartLimitIntervalSec=120
StartLimitBurst=3

[Service]
ExecStart={BIN}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=steamvr.service
"""


def _systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=30)


def service_active():
    return _systemctl("is-active", "--quiet", UNIT).returncode == 0


def apply_service(c, restart=False):
    """Runs the overlay only while the indicator is on and some mode is chosen.
    restart=True restarts a running overlay (e.g. so it picks up new marker pictures).
    Returns (ok, message)."""
    want = bool(c.get("enabled")) and any_mode(c)
    if not want:
        _systemctl("disable", "--now", UNIT)
        return True, "off"
    if not os.access(BIN, os.X_OK):
        return False, "ceiling_overlay is missing: run helpers/build.sh on the Frame"
    try:
        with open(UNIT_PATH, encoding="utf-8") as f:
            current = f.read()
    except OSError:
        current = None
    if current != UNIT_TEXT:
        os.makedirs(os.path.dirname(UNIT_PATH), exist_ok=True)
        tmp = UNIT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(UNIT_TEXT)
        os.replace(tmp, UNIT_PATH)
        _systemctl("daemon-reload")
        _systemctl("disable", UNIT)  # drops links made by an older unit (it was wanted by default.target)
    _systemctl("reset-failed", UNIT)  # after 3 quick crashes systemd refuses to start it until this
    r = _systemctl("enable", "--now", UNIT)
    if restart:
        _systemctl("try-restart", UNIT)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip() or "systemd refused to start it"
    time.sleep(0.5)
    if service_active():
        return True, "on"
    return False, "it starts together with SteamVR" if not _steamvr_up() else "it stopped right away (see logs)"


def _steamvr_up():
    return _systemctl("is-active", "--quiet", "steamvr.service").returncode == 0


def reclined():
    """True while Recline VR is tilting the world (from its shared memory; no SteamVR call)."""
    try:
        with open("/dev/shm/recline_v1", "rb") as f:
            d = f.read(256)
        magic, = struct.unpack_from("<I", d, 0)
        flags, = struct.unpack_from("<I", d, 192 + 12)
        return magic == 0x52434C4E and bool(flags & 2)
    except (OSError, struct.error):
        return False


def measure(seconds=5.0):
    """Highest point a controller reaches in `seconds` (standing space, metres above the floor),
    plus the play-area centre. Hold a controller flat against the ceiling meanwhile."""
    if reclined():
        raise RuntimeError("Recline VR is tilting the world: sit or stand up first (it resets itself)")
    p = subprocess.Popen([SPACE_TOOL, "track", "100"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         text=True, env=paths.clean_env())
    # Stop the tool at the deadline: its output then ends and the loop below finishes. The kill is a
    # backstop for a tool stuck connecting to SteamVR.
    timers = [threading.Timer(seconds, p.terminate), threading.Timer(seconds + 4, p.kill)]
    for t in timers:
        t.daemon = True
        t.start()
    top, seen, lines = None, 0, 0
    try:
        for line in p.stdout:
            lines += 1
            parts = line.split()
            if len(parts) == 8 and parts[0] == "p" and parts[1] in ("left", "right") and parts[2] == "1":
                try:
                    y = float(parts[4])
                except ValueError:
                    continue
                if y == y and abs(y) < 10:  # not NaN, not nonsense
                    seen += 1
                    top = y if top is None else max(top, y)
    finally:
        for t in timers:
            t.cancel()
        if p.poll() is None:
            p.terminate()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
    if top is None:
        if not lines:
            raise RuntimeError("SteamVR isn't running")
        raise RuntimeError("no controller was tracked: turn one on and hold it up")
    return round(top + 0.02, 2), seen  # + half a controller's thickness


def play_area_centre():
    try:
        r = subprocess.run([SPACE_TOOL, "bounds"], capture_output=True, text=True, timeout=15,
                           env=paths.clean_env())
        pts = [(float(a), float(b)) for a, b in re.findall(r"^b (\S+) (\S+)$", r.stdout, re.M)]
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pts = []
    if not pts:
        return 0.0, 0.0
    return (round((min(p[0] for p in pts) + max(p[0] for p in pts)) / 2, 2),
            round((min(p[1] for p in pts) + max(p[1] for p in pts)) / 2, 2))
