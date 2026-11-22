"""Signal map for Deckhand: find the best spot to play wirelessly.

The Frame has two separate radio links:
  home Wi-Fi    the Frame's link to your router (iw <station iface> link). Used for the
                internet, the store, and PC streaming without the dongle.
  Steam dongle  the PC's USB dongle talking to the Frame's own 6 GHz access point
                (iw <ap iface> station dump). Used for PC VR streaming with the dongle.
While you walk around, a Recorder pairs the headset's position (space_tool track) with
the strength of both links, twice a second. The samples are averaged into 0.5 m squares
and drawn over your boundary; the strongest square is the best place to stand.
Nothing runs unless you are mapping (or a map page shows where you are).
"""
import json
import math
import os
import re
import subprocess
import threading
import time

import paths

SPACE_TOOL = paths.tool("space_tool")
MAPS = os.path.expanduser("~/.local/share/frame-settings/wifi")
CELL = 0.5          # metres per square
MAX_SECONDS = 300   # mapping stops by itself after 5 min
SAMPLE_EVERY = 0.5  # seconds
IW_CACHE = 30.0     # seconds an `iw dev` interface list stays valid


# ------------------------------------------------------------ radio links ---
def _iw(*args, timeout=3):
    try:
        return subprocess.run(["iw", *args], capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


_IFACES = {"at": -1e9, "found": {}}


def interfaces(fresh=False):
    """{interface: type} from `iw dev` ("managed" = Wi-Fi client, "AP" = the dongle's access point).
    Cached for 30 s: the list hardly ever changes and this is asked twice a second while mapping."""
    now = time.monotonic()
    if fresh or now - _IFACES["at"] > IW_CACHE:
        found, iface = {}, None
        for line in _iw("dev").splitlines():
            line = line.strip()
            if line.startswith("Interface "):
                iface = line.split()[1]
            elif line.startswith("type ") and iface:
                found[iface] = line.split(None, 1)[1]
        _IFACES.update(at=now, found=found)
    return _IFACES["found"]


def station_interface():
    """The Wi-Fi interface used as a client of a network (not the dongle's access point)."""
    return next((i for i, t in interfaces().items() if t == "managed"), "wlan0")


def ap_interface():
    """The Frame's own access point (the dongle's link), or None."""
    return next((i for i, t in interfaces().items() if t == "AP"), None)


def _num(pattern, text):
    m = re.search(pattern, text)
    return float(m.group(1)) if m else None


def signal(iface=None):
    """{'dbm', 'tx', 'rx', 'freq', 'ssid', 'iface'} of the current Wi-Fi link, or None if not connected."""
    iface = iface or station_interface()
    out = _iw("dev", iface, "link")
    if "Connected" not in out:
        return None
    ssid = re.search(r"SSID:\s*(.+)", out)
    return {"dbm": _num(r"signal:\s*(-?\d+)", out), "tx": _num(r"tx bitrate:\s*([\d.]+)", out),
            "rx": _num(r"rx bitrate:\s*([\d.]+)", out), "freq": _num(r"freq:\s*([\d.]+)", out),
            "ssid": ssid.group(1).strip() if ssid else "", "iface": iface}


def dongle_signal(iface=None):
    """{'dbm', 'tx', 'rx', 'freq', 'ssid', 'iface'} of the Steam dongle's link to the Frame, or None if
    not connected. Strength = how loudly the Frame hears the dongle's acknowledgements (the plain
    "signal" field reads 0 on this access point)."""
    iface = iface or ap_interface()
    if not iface:
        return None
    best = None
    for block in _iw("dev", iface, "station", "dump").split("Station ")[1:]:
        dbm = (_num(r"avg ack signal:\s*(-?\d+)", block) or _num(r"last ack signal:\s*(-?\d+)", block)
               or _num(r"signal avg:\s*(-?\d+)", block))
        if not dbm:  # None or 0: no real reading
            continue
        if (_num(r"inactive time:\s*(\d+)", block) or 0) > 30000:  # gone quiet: not really connected
            continue
        s = {"dbm": dbm, "tx": _num(r"tx bitrate:\s*([\d.]+)", block), "rx": _num(r"rx bitrate:\s*([\d.]+)", block),
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


# -------------------------------------------------------------- tracking ---
def live_bounds():
    """Boundary outline on the floor right now [(x, z), ...] (standing space), or []."""
    try:
        r = subprocess.run([SPACE_TOOL, "bounds"], capture_output=True, text=True, timeout=15,
                           env=paths.clean_env())
        return [(float(a), float(b)) for a, b in re.findall(r"^b (\S+) (\S+)$", r.stdout, re.M)]
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []


class PoseWatcher:
    """Where the headset is right now: space_tool track as a background SteamVR app (it never starts
    SteamVR). pose = (x, y, z) in standing space, or None while the headset isn't tracked;
    heading = the look direction on the floor (x, z). stop() ends it."""

    def __init__(self, every_ms=500):
        self.pose = None
        self.heading = (0.0, -1.0)
        self.every_ms = every_ms
        self.proc = None
        self.running = False
        self.ended = False   # the tool quit by itself (SteamVR stopped?): no more positions
        self._wake = threading.Event()  # set by stop(): wakes a sampler thread from its pause

    def start(self):
        if not os.access(SPACE_TOOL, os.X_OK):
            raise RuntimeError("space_tool is missing: run helpers/build.sh on the Frame")
        self.pose, self.ended = None, False
        self._wake.clear()
        self.proc = subprocess.Popen([SPACE_TOOL, "track", str(self.every_ms)], stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, env=paths.clean_env())
        self.running = True
        threading.Thread(target=self._read_poses, args=(self.proc,), daemon=True).start()
        return self

    def _read_poses(self, proc):
        try:
            for line in proc.stdout:
                if not self.running or proc is not self.proc:
                    break
                p = line.split()
                if len(p) != 8 or p[0] != "p" or p[1] != "hmd":
                    continue
                try:
                    x, y, z, fx, fz = (float(v) for v in p[3:8])
                except ValueError:
                    continue
                if p[2] != "1" or not all(map(math.isfinite, (x, y, z, fx, fz))):
                    self.pose = None  # not tracked right now
                    continue
                self.pose = (x, y, z)
                if math.hypot(fx, fz) > 0.2:  # looking (nearly) straight up or down: keep the last heading
                    self.heading = (fx, fz)
        except (OSError, ValueError):
            pass
        if proc is self.proc:  # ended by itself, not by stop(): a stale position would mislead
            self.pose = None
            self.ended = self.running

    def alive(self):
        return bool(self.running and self.proc and not self.ended)

    def stop(self):
        self.running = False
        self._wake.set()
        proc, self.proc = self.proc, None
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            if proc.stdout:
                proc.stdout.close()
        return self


class Recorder(PoseWatcher):
    """Play-space walk. start() → samples both links twice a second (thread); stop() → map dict (saved)."""

    def __init__(self):
        super().__init__(every_ms=200)
        self.samples = []      # [x, z, wifi dbm, wifi tx, dongle dbm, dongle tx] (None = not connected)
        self.latest = None     # latest signal()
        self.latest_dongle = None
        self.started = 0.0
        self.bounds = []
        self.iface = self.ap = None
        self._sampler = None

    def start(self):
        """Slow (reads the boundary from SteamVR): call it off the UI thread."""
        if not os.access(SPACE_TOOL, os.X_OK):
            raise RuntimeError("space_tool is missing: run helpers/build.sh on the Frame")
        self.iface, self.ap = station_interface(), ap_interface()
        self.latest, self.latest_dongle = signal(self.iface), dongle_signal(self.ap)
        if not self.latest and not self.latest_dongle:
            raise RuntimeError("neither home Wi-Fi nor the Steam dongle is connected")
        self.bounds = live_bounds()
        self.samples = []
        super().start()
        self.started = time.monotonic()
        self._sampler = threading.Thread(target=self._sample, daemon=True)
        self._sampler.start()
        return self

    def _sample(self):
        while self.running:
            if self.elapsed() > MAX_SECONDS:
                self.running = False  # the app sees this and finishes the map
                break
            s = signal(self.iface)
            d = dongle_signal(self.ap) if self.ap else None
            self.latest, self.latest_dongle = s, d
            s = s if s and s["dbm"] is not None else None
            pose = self.pose
            if (s or d) and pose:
                self.samples.append([round(pose[0], 2), round(pose[2], 2),
                                     s["dbm"] if s else None, s["tx"] if s else None,
                                     d["dbm"] if d else None, d["tx"] if d else None])
            self._wake.wait(SAMPLE_EVERY)

    def elapsed(self):
        return time.monotonic() - self.started if self.started else 0

    def stop(self):
        super().stop()
        if self._sampler and self._sampler is not threading.current_thread():
            self._sampler.join(timeout=1)  # its last reading lands before the map is made
        samples = list(self.samples)
        m = {"saved": time.time(), "bounds": self.bounds, "samples": samples,
             "ssid": (self.latest or {}).get("ssid", ""), "freq": (self.latest or {}).get("freq")}
        m.update(summarise(m))
        if samples:
            save(m)
        return m


# -------------------------------------------------------------- analysis ---
def grid(samples, target="wifi"):
    """{(i, j): (avg dBm, avg Mbit/s, count)} for 0.5 m squares, for one link."""
    k = 4 if target == "dongle" else 2
    cells = {}
    for smp in samples:
        if len(smp) <= k:
            continue
        x, z, dbm = smp[0], smp[1], smp[k]
        if dbm is None or not (math.isfinite(x) and math.isfinite(z)):
            continue
        tx = smp[k + 1] if len(smp) > k + 1 else None
        c = cells.setdefault((int(x // CELL), int(z // CELL)), [0.0, 0.0, 0])
        c[0] += dbm
        c[1] += tx or 0
        c[2] += 1
    return {key: (v[0] / v[2], v[1] / v[2], v[2]) for key, v in cells.items()}


def summarise(m):
    """Best / worst / average for home Wi-Fi at the top level (older maps), and per link under
    m["links"]["wifi"] and m["links"]["dongle"]."""
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


# --------------------------------------------------------------- storage ---
def save(m):
    os.makedirs(MAPS, exist_ok=True)
    path = os.path.join(MAPS, time.strftime("map-%Y%m%d-%H%M%S.json"))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(m, f)
    os.replace(tmp, path)
    return path


def clear():
    """Start fresh: saved maps are renamed out of the way (kept as .cleared backups, the newest 20)."""
    if not os.path.isdir(MAPS):
        return
    for f in os.listdir(MAPS):
        if f.endswith(".json"):
            os.replace(os.path.join(MAPS, f), os.path.join(MAPS, f + ".cleared"))
    old = sorted(f for f in os.listdir(MAPS) if f.endswith(".cleared"))
    for f in old[:-20]:
        try:
            os.remove(os.path.join(MAPS, f))
        except OSError:
            pass


def last_map():
    """The newest saved map that reads correctly, or None."""
    try:
        files = sorted((f for f in os.listdir(MAPS) if f.endswith(".json")), reverse=True)
    except OSError:
        return None
    for name in files:
        try:
            with open(os.path.join(MAPS, name), encoding="utf-8") as f:
                m = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(m, dict):
            return m
    return None
