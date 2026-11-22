"""Signal map for Deckhand: find the best spot to play wirelessly.

The Frame has two separate radio links:
  home Wi-Fi    the Frame's link to your router (iw <station iface> link). Used for the
                internet, the store, and PC streaming without the dongle.
  Steam dongle  the PC's USB dongle talking to the Frame's own 6 GHz access point
                (iw <ap iface> station dump). Used for PC VR streaming with the dongle.
While you walk around, a Recorder pairs the headset's position (space_tool track) with
the strength of both links, twice a second. The samples are averaged into 0.5 m squares
and drawn over your boundary; the strongest square is the best place to stand.
Nothing runs unless you are mapping.
"""
import json
import os
import re
import subprocess
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SPACE_TOOL = os.path.join(HERE, "space_tool")
MAPS = os.path.expanduser("~/.local/share/frame-settings/wifi")
CELL = 0.5          # metres per square
MAX_SECONDS = 300   # mapping stops by itself after 5 min
SAMPLE_EVERY = 0.5  # seconds


def station_interface():
    """The Wi-Fi interface connected to a network as a client (not the dongle's access point)."""
    r = subprocess.run(["iw", "dev"], capture_output=True, text=True)
    iface = None
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("Interface "):
            iface = line.split()[1]
        elif line == "type managed" and iface:
            return iface
    return "wlan0"


def signal(iface=None):
    """{'dbm', 'tx', 'rx', 'freq', 'ssid'} of the current Wi-Fi link, or None if not connected."""
    iface = iface or station_interface()
    r = subprocess.run(["iw", "dev", iface, "link"], capture_output=True, text=True)
    if "Connected" not in r.stdout:
        return None

    def num(pattern):
        m = re.search(pattern, r.stdout)
        return float(m.group(1)) if m else None
    ssid = re.search(r"SSID:\s*(.+)", r.stdout)
    return {"dbm": num(r"signal:\s*(-?\d+)"), "tx": num(r"tx bitrate:\s*([\d.]+)"),
            "rx": num(r"rx bitrate:\s*([\d.]+)"), "freq": num(r"freq:\s*([\d.]+)"),
            "ssid": ssid.group(1).strip() if ssid else "", "iface": iface}


def ap_interface():
    """The Frame's own access point (the dongle's link), or None."""
    r = subprocess.run(["iw", "dev"], capture_output=True, text=True)
    iface = None
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("Interface "):
            iface = line.split()[1]
        elif line == "type AP" and iface:
            return iface
    return None


def dongle_signal(iface=None):
    """{'dbm', 'tx', 'rx', 'freq'} of the Steam dongle's link to the Frame, or None if not connected.
    Strength = how loudly the Frame hears the dongle's acknowledgements (the plain "signal" field
    reads 0 on this access point)."""
    iface = iface or ap_interface()
    if not iface:
        return None
    r = subprocess.run(["iw", "dev", iface, "station", "dump"], capture_output=True, text=True)
    best = None
    for block in r.stdout.split("Station ")[1:]:
        def num(pattern):
            m = re.search(pattern, block)
            return float(m.group(1)) if m else None
        dbm = num(r"avg ack signal:\s*(-?\d+)") or num(r"last ack signal:\s*(-?\d+)") or num(r"signal avg:\s*(-?\d+)")
        if dbm is None or dbm == 0:
            continue
        if (num(r"inactive time:\s*(\d+)") or 0) > 30000:  # gone quiet: not really connected
            continue
        s = {"dbm": dbm, "tx": num(r"tx bitrate:\s*([\d.]+)"), "rx": num(r"rx bitrate:\s*([\d.]+)"),
             "freq": 6000, "ssid": "Steam dongle", "iface": iface}
        if best is None or dbm > best["dbm"]:
            best = s
    return best


TARGETS = {"wifi": "Home Wi-Fi", "dongle": "Steam dongle"}


# Signal colours: the "viridis" scale (dark purple = weak → blue → teal → green → bright yellow =
# strong). It changes steadily in brightness, so it reads the same with red-green colour blindness.
DBM_LOW, DBM_HIGH = -90.0, -30.0
VIRIDIS = ["#440154", "#482475", "#414487", "#355f8d", "#2a788e", "#21918c",
           "#22a884", "#44bf70", "#7ad151", "#bddf26", "#fde725"]
WORDS = ((-50, "excellent"), (-60, "good"), (-67, "fair"), (-75, "weak"))


def _rgb(h):
    return [int(h[i:i + 2], 16) for i in (1, 3, 5)]


def strength(dbm):
    """0 (-90 dBm or worse) … 1 (-30 dBm or better)."""
    return max(0.0, min(1.0, (dbm - DBM_LOW) / (DBM_HIGH - DBM_LOW)))


def fill(dbm):
    """Map colour for a signal strength (colour-blind safe gradient)."""
    t = strength(dbm) * (len(VIRIDIS) - 1)
    i = min(int(t), len(VIRIDIS) - 2)
    a, b, f = _rgb(VIRIDIS[i]), _rgb(VIRIDIS[i + 1]), t - i
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * f) for x, y in zip(a, b))


def quality(dbm):
    """(word, text colour) for a signal strength. The colour is the map gradient, lightened at the
    weak end so it stays readable on the dark background. For streaming VR, aim for -60 dBm or better."""
    if dbm is None:
        return "no signal", "#8a96a3"
    word = next((w for limit, w in WORDS if dbm >= limit), "poor")
    t = strength(dbm)
    mix = 0.45 * (1 - t)  # blend toward white at the dark end
    return word, "#%02x%02x%02x" % tuple(round(c + (255 - c) * mix) for c in _rgb(fill(dbm)))


def legend_svg(x, y, width, gid="sig"):
    """A gradient bar with dBm ticks and words, for the bottom of a map (about 56 px tall)."""
    stops = "".join(f'<stop offset="{i / (len(VIRIDIS) - 1):.2f}" stop-color="{c}"/>' for i, c in enumerate(VIRIDIS))
    parts = [f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="1" y2="0">{stops}</linearGradient></defs>',
             f'<rect x="{x}" y="{y}" width="{width}" height="16" rx="8" fill="url(#{gid})"/>']
    for dbm in range(-90, -29, 10):
        tx = x + width * strength(dbm)
        anchor = "start" if dbm == -90 else "end" if dbm == -30 else "middle"
        parts.append(f'<line x1="{tx:.1f}" y1="{y + 16}" x2="{tx:.1f}" y2="{y + 22}" stroke="#c9d6e3" stroke-width="2"/>'
                     f'<text x="{tx:.1f}" y="{y + 40}" fill="#c9d6e3" font-size="17" text-anchor="{anchor}" '
                     f'font-family="sans-serif">{dbm}</text>')
    parts.append(f'<text x="{x}" y="{y - 6}" fill="#c9d6e3" font-size="17" font-family="sans-serif">weak</text>'
                 f'<text x="{x + width}" y="{y - 6}" fill="#c9d6e3" font-size="17" text-anchor="end" '
                 f'font-family="sans-serif">strong (dBm)</text>')
    return "".join(parts)


def band(freq):
    if not freq:
        return ""
    return "6 GHz" if freq >= 5925 else "5 GHz" if freq >= 4900 else "2.4 GHz"


def live_bounds():
    """Boundary outline on the floor right now [(x, z), ...] (standing space), or []."""
    try:
        r = subprocess.run([SPACE_TOOL, "bounds"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [(float(a), float(b)) for a, b in re.findall(r"^b (\S+) (\S+)$", r.stdout, re.M)]


class Recorder:
    """Walk-around recording. start() → (thread) samples; stop() → map dict (also saved)."""

    def __init__(self):
        self.samples = []      # [x, z, wifi dbm, wifi tx, dongle dbm, dongle tx] (None = not connected)
        self.pose = None       # latest headset (x, z) or None
        self.latest = None     # latest signal()
        self.latest_dongle = None
        self.running = False
        self.started = 0.0
        self.proc = None
        self.bounds = []
        self.iface = self.ap = None

    def start(self):
        if not os.access(SPACE_TOOL, os.X_OK):
            raise RuntimeError("space_tool is missing: run build.sh on the Frame")
        self.iface, self.ap = station_interface(), ap_interface()
        if not signal(self.iface) and not dongle_signal(self.ap):
            raise RuntimeError("neither home Wi-Fi nor the Steam dongle is connected")
        self.bounds = live_bounds()
        self.proc = subprocess.Popen([SPACE_TOOL, "track", "200"], stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True)
        self.samples, self.pose, self.running, self.started = [], None, True, time.time()
        threading.Thread(target=self._read_poses, daemon=True).start()
        threading.Thread(target=self._sample, daemon=True).start()

    def _read_poses(self):
        for line in self.proc.stdout:
            parts = line.split()
            if len(parts) == 8 and parts[0] == "p" and parts[1] == "hmd":
                self.pose = (float(parts[3]), float(parts[5])) if parts[2] == "1" else None
            if not self.running:
                break

    def _sample(self):
        while self.running:
            if time.time() - self.started > MAX_SECONDS:
                self.running = False
                break
            s = signal(self.iface)
            d = dongle_signal(self.ap) if self.ap else None
            self.latest, self.latest_dongle = s, d
            s = s if s and s["dbm"] is not None else None
            if (s or d) and self.pose:
                self.samples.append([round(self.pose[0], 2), round(self.pose[1], 2),
                                     s["dbm"] if s else None, s["tx"] if s else None,
                                     d["dbm"] if d else None, d["tx"] if d else None])
            time.sleep(SAMPLE_EVERY)

    def elapsed(self):
        return time.time() - self.started if self.started else 0

    def stop(self):
        self.running = False
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
        m = {"saved": time.time(), "bounds": self.bounds, "samples": self.samples,
             "ssid": (self.latest or {}).get("ssid", ""), "freq": (self.latest or {}).get("freq")}
        m.update(summarise(m))
        if self.samples:
            save(m)
        return m


def grid(samples, target="wifi"):
    """{(i, j): (avg dBm, avg Mbit/s, count)} for 0.5 m squares, for one link."""
    k = 4 if target == "dongle" else 2
    cells = {}
    for smp in samples:
        x, z = smp[0], smp[1]
        dbm = smp[k] if len(smp) > k else None
        tx = smp[k + 1] if len(smp) > k + 1 else None
        if dbm is None:
            continue
        key = (int((x // CELL)), int((z // CELL)))
        c = cells.setdefault(key, [0.0, 0.0, 0])
        c[0] += dbm
        c[1] += tx or 0
        c[2] += 1
    return {k: (v[0] / v[2], v[1] / v[2], v[2]) for k, v in cells.items()}


def summarise(m):
    """Best / worst / average for home Wi-Fi at the top level (older maps), and per link under
    m["wifi"] and m["dongle"]."""
    out = summarise_one(m, "wifi")
    out["links"] = {t: summarise_one(m, t) for t in TARGETS}
    return out


def summarise_one(m, target):
    cells = grid(m.get("samples", []), target)
    if not cells:
        return {"best": None, "worst": None, "avg": None, "cells": 0}
    best = max(cells.items(), key=lambda kv: kv[1][0])
    worst = min(cells.items(), key=lambda kv: kv[1][0])
    avg = sum(v[0] * v[2] for v in cells.values()) / sum(v[2] for v in cells.values())

    def centre(key):
        return [round((key[0] + 0.5) * CELL, 2), round((key[1] + 0.5) * CELL, 2)]
    return {"best": {"at": centre(best[0]), "dbm": round(best[1][0], 1), "tx": round(best[1][1])},
            "worst": {"at": centre(worst[0]), "dbm": round(worst[1][0], 1), "tx": round(worst[1][1])},
            "avg": round(avg, 1), "cells": len(cells)}


def where(at):
    """Plain words for a spot in standing space, relative to where the room starts (0, 0, facing -z)."""
    x, z = at
    parts = []
    if abs(x) >= 0.25:
        parts.append(f"{abs(x):.1f} m {'right' if x > 0 else 'left'}")
    if abs(z) >= 0.25:
        parts.append(f"{abs(z):.1f} m {'behind' if z > 0 else 'ahead'}")
    return ", ".join(parts) or "right where the room starts"


def save(m):
    os.makedirs(MAPS, exist_ok=True)
    path = os.path.join(MAPS, time.strftime("map-%Y%m%d-%H%M%S.json"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(m, f)
    return path


def clear():
    """Start fresh: saved maps are renamed out of the way (kept as .cleared backups)."""
    if not os.path.isdir(MAPS):
        return
    for f in os.listdir(MAPS):
        if f.endswith(".json"):
            os.replace(os.path.join(MAPS, f), os.path.join(MAPS, f + ".cleared"))


def last_map():
    try:
        files = sorted(f for f in os.listdir(MAPS) if f.endswith(".json"))
        with open(os.path.join(MAPS, files[-1]), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, IndexError):
        return None
