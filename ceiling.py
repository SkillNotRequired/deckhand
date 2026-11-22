"""Ceiling indicator for Deckhand.

Settings live in ~/.config/frame-settings/ceiling.conf (key=value) and are read by
the native overlay ceiling_overlay, run as the user service frame-ceiling.service
only while at least one mode is on:
  near      grid at the ceiling fades in when a controller or your head gets close
  always    the grid stays faintly visible
  upmarker  while Recline VR tilts the world, markers show the real ceiling and floor

measure() finds the ceiling height from a controller held against it (while not reclined).
"""
import os
import re
import struct
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.expanduser("~/.config/frame-settings/ceiling.conf")
DATA = os.path.expanduser("~/.local/share/frame-settings/ceiling")
SPACE_TOOL = os.path.join(HERE, "space_tool")
BIN = os.path.join(HERE, "ceiling_overlay")
UNIT = "frame-ceiling.service"
UNIT_PATH = os.path.expanduser(f"~/.config/systemd/user/{UNIT}")
DEFAULTS = {"height": 2.40, "cx": 0.0, "cz": 0.0, "near": 1, "near_dist": 0.40, "always": 0,
            "always_alpha": 0.18, "upmarker": 1, "enabled": 0, "measured": 0}


def load():
    c = dict(DEFAULTS)
    try:
        with open(CONF, encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    if k in c:
                        c[k] = float(v) if "." in v or k in ("height", "cx", "cz") else int(float(v))
    except (OSError, ValueError):
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

[Service]
ExecStart={BIN}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def service_active():
    return subprocess.run(["systemctl", "--user", "is-active", "--quiet", UNIT]).returncode == 0


def apply_service(c):
    """Runs the overlay only while the indicator is on and some mode is chosen."""
    want = bool(c.get("enabled")) and any_mode(c)
    if want:
        if not os.access(BIN, os.X_OK):
            return False, "ceiling_overlay is missing: run build.sh on the Frame"
        current = None
        try:
            with open(UNIT_PATH, encoding="utf-8") as f:
                current = f.read()
        except OSError:
            pass
        if current != UNIT_TEXT:
            os.makedirs(os.path.dirname(UNIT_PATH), exist_ok=True)
            with open(UNIT_PATH, "w", encoding="utf-8") as f:
                f.write(UNIT_TEXT)
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", UNIT], capture_output=True)
        return service_active(), "on"
    subprocess.run(["systemctl", "--user", "disable", "--now", UNIT], capture_output=True)
    return True, "off"


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
    p = subprocess.Popen([SPACE_TOOL, "track", "100"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    top, seen = None, 0
    end = time.time() + seconds
    try:
        for line in p.stdout:
            parts = line.split()
            if len(parts) == 8 and parts[0] == "p" and parts[1] in ("left", "right") and parts[2] == "1":
                y = float(parts[4])
                seen += 1
                top = y if top is None else max(top, y)
            if time.time() > end:
                break
    finally:
        p.terminate()
    if top is None:
        raise RuntimeError("no controller was tracked: turn one on and hold it up")
    return round(top + 0.02, 2), seen  # + half a controller's thickness


def play_area_centre():
    try:
        r = subprocess.run([SPACE_TOOL, "bounds"], capture_output=True, text=True, timeout=15)
        pts = [(float(a), float(b)) for a, b in re.findall(r"^b (\S+) (\S+)$", r.stdout, re.M)]
    except (OSError, subprocess.TimeoutExpired):
        pts = []
    if not pts:
        return 0.0, 0.0
    return (round((min(p[0] for p in pts) + max(p[0] for p in pts)) / 2, 2),
            round((min(p[1] for p in pts) + max(p[1] for p in pts)) / 2, 2))
