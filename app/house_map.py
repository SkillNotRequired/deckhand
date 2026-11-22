"""Whole-house map for Deckhand: the home's layout plus Wi-Fi and Steam-dongle signal.

Walk the house with the headset on (several walks add up into one map). Twice a second
a HouseRecorder stores where the headset is (space_tool track, standing space) and the
strength of BOTH links:
  home Wi-Fi    the Frame's link to your router (iw <station iface> link)
  Steam dongle  the PC dongle's link to the Frame's own 6 GHz access point
                (iw <ap iface> station dump: how loudly the Frame hears the dongle)

Heights split floors. Results: a top-down map per floor (0.5 m squares coloured by the
chosen signal, the edge of where you walked, your path, labels).
Tracking can jump if the Frame re-recognises a room; a jump over 2 m starts a new
path segment instead of drawing a line across the house.
"""
import html
import json
import math
import os
import subprocess
import threading
import time

import wifi_map

HOUSE = os.path.expanduser("~/.local/share/frame-settings/wifi-house")
CURRENT = os.path.join(HOUSE, "current.json")
CELL = 0.5
MAX_SECONDS = 3600
FLOOR_HEIGHT = 2.7   # metres between storeys (for splitting floors)
EYE_HEIGHT = 1.6     # head above its floor, roughly
LABELS = ["Router", "PC", "Bedroom", "Kitchen", "Living room", "Office", "Bathroom", "Garage", "Hall"]
TARGETS = wifi_map.TARGETS

# sample: [x, y, z, wifi_dbm, wifi_tx, segment, dongle_dbm, dongle_tx]
WIFI, WIFI_TX, SEG, DONGLE, DONGLE_TX = 3, 4, 5, 6, 7


ap_interface = wifi_map.ap_interface
dongle_signal = wifi_map.dongle_signal


class PoseWatcher:
    """Where the headset is right now (for the "You" marker while the map page is open):
    space_tool track twice a second as a background SteamVR app; stop() ends it."""

    def __init__(self, every_ms=500):
        self.pose = None                    # latest headset (x, y, z) or None
        self.heading = (0.0, -1.0)          # look direction on the floor (x, z)
        self.every_ms = every_ms
        self.proc = None
        self.running = False

    def start(self):
        if not os.access(wifi_map.SPACE_TOOL, os.X_OK):
            raise RuntimeError("space_tool is missing: run helpers/build.sh on the Frame")
        self.proc = subprocess.Popen([wifi_map.SPACE_TOOL, "track", str(self.every_ms)],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.running = True
        threading.Thread(target=self._read_poses, daemon=True).start()
        return self

    def _read_poses(self):
        proc = self.proc
        for line in proc.stdout:
            p = line.split()
            if len(p) == 8 and p[0] == "p" and p[1] == "hmd":
                pos = (float(p[3]), float(p[4]), float(p[5])) if p[2] == "1" else None
                self.pose = pos
                fx, fz = float(p[6]), float(p[7])
                if pos and math.hypot(fx, fz) > 0.2:
                    self.heading = (fx, fz)
            if not self.running:
                break

    def stop(self):
        self.running = False
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
        return self


class HouseRecorder(PoseWatcher):
    """A house walk: the headset's position (5 times a second) and both links' signal (twice a second)."""

    def __init__(self):
        super().__init__(every_ms=200)
        self.samples = []
        self.latest = None                  # latest home Wi-Fi reading
        self.latest_dongle = None
        self.started = 0.0
        self.distance = 0.0
        self.segment = 0
        self.iface = self.ap = None
        self._last = None

    def start(self, segment_offset=0):
        self.iface, self.ap = wifi_map.station_interface(), ap_interface()
        self.segment = segment_offset
        super().start()
        self.started = time.time()
        threading.Thread(target=self._sample, daemon=True).start()
        return self

    def _sample(self):
        while self.running:
            if time.time() - self.started > MAX_SECONDS:
                self.running = False
                break
            s = wifi_map.signal(self.iface)
            d = dongle_signal(self.ap) if self.ap else None
            self.latest, self.latest_dongle = s, d
            pose = self.pose
            if pose and ((s and s["dbm"] is not None) or d):
                if self._last:
                    step = math.dist(pose, self._last)
                    if step > 2.0:
                        self.segment += 1  # tracking jumped: don't join it up
                    else:
                        self.distance += step
                self._last = pose
                self.samples.append([round(pose[0], 2), round(pose[1], 2), round(pose[2], 2),
                                     s["dbm"] if s else None, s["tx"] if s else None, self.segment,
                                     d["dbm"] if d else None, d["tx"] if d else None])
            time.sleep(0.5)

    def elapsed(self):
        return time.time() - self.started if self.started else 0


def add_walk(m, rec):
    """Adds a finished walk to the house map `m` (or starts one)."""
    m = m or {"kind": "house", "samples": [], "labels": [], "distance": 0.0, "walks": 0}
    m["samples"] += rec.samples
    m["distance"] = round(m.get("distance", 0) + rec.distance, 1)
    m["walks"] = m.get("walks", 0) + 1
    m["saved"] = time.time()
    m["ssid"] = (rec.latest or {}).get("ssid", m.get("ssid", ""))
    return m


# ------------------------------------------------------------- analysis ---
def floor_of(y, base):
    return int(round((y - base) / FLOOR_HEIGHT))


def floor_base(samples):
    """Typical head height on the floor where the first walk started."""
    ys = sorted(s[1] for s in samples[:40]) or [EYE_HEIGHT]
    return ys[len(ys) // 2]


def value(s, target):
    i = DONGLE if target == "dongle" else WIFI
    return s[i] if len(s) > i else None


def rate(s, target):
    i = DONGLE_TX if target == "dongle" else WIFI_TX
    return (s[i] if len(s) > i else None) or 0


def cells(samples, target="wifi"):
    """{(floor, i, j): (avg dBm, avg Mbit/s, count)} for the chosen signal."""
    base = floor_base(samples)
    acc = {}
    for s in samples:
        v = value(s, target)
        if v is None:
            continue
        k = (floor_of(s[1], base), int(s[0] // CELL), int(s[2] // CELL))
        a = acc.setdefault(k, [0.0, 0.0, 0])
        a[0] += v
        a[1] += rate(s, target)
        a[2] += 1
    return {k: (v[0] / v[2], v[1] / v[2], v[2]) for k, v in acc.items()}


def footprint(samples):
    """{(floor, i, j)} squares walked (whatever was measured there)."""
    base = floor_base(samples)
    return {(floor_of(s[1], base), int(s[0] // CELL), int(s[2] // CELL)) for s in samples}


def outline_edges(samples, floor):
    """Edges of the walked area on one floor (where a walked square meets an unwalked one): the
    fallback outline of the rooms. [(x0, z0, x1, z1)]"""
    sq = {(i, j) for f, i, j in footprint(samples) if f == floor}
    edges = []
    for i, j in sq:
        x0, z0, x1, z1 = i * CELL, j * CELL, (i + 1) * CELL, (j + 1) * CELL
        if (i - 1, j) not in sq:
            edges.append((x0, z0, x0, z1))
        if (i + 1, j) not in sq:
            edges.append((x1, z0, x1, z1))
        if (i, j - 1) not in sq:
            edges.append((x0, z0, x1, z0))
        if (i, j + 1) not in sq:
            edges.append((x0, z1, x1, z1))
    return edges


def floors(samples):
    base = floor_base(samples)
    return sorted({floor_of(s[1], base) for s in samples})


def floor_name(f):
    return "Starting floor" if f == 0 else (f"{f} up" if f > 0 else f"{-f} down")


def nearest_label(m, floor, x, z, within=4.0):
    best = None
    for lab in m.get("labels", []):
        if lab.get("floor", 0) != floor:
            continue
        d = math.hypot(lab["x"] - x, lab["z"] - z)
        if d <= within and (best is None or d < best[0]):
            best = (d, lab["name"])
    return best[1] if best else None


def summary(m, target="wifi"):
    cs = cells(m.get("samples", []), target)
    if not cs:
        return None
    best = max(cs.items(), key=lambda kv: kv[1][0])
    worst = min(cs.items(), key=lambda kv: kv[1][0])
    n = sum(v[2] for v in cs.values())
    avg = sum(v[0] * v[2] for v in cs.values()) / n
    weak = sum(1 for v in cs.values() if v[0] < -67)
    multi = len(floors(m["samples"])) > 1

    def describe(kv):
        (f, i, j), (dbm, tx, _n) = kv
        x, z = (i + 0.5) * CELL, (j + 0.5) * CELL
        near = nearest_label(m, f, x, z)
        where = f"near {near}" if near else wifi_map.where([x, z]) + " from where you started"
        if multi:
            where += f" ({floor_name(f).lower()})"
        return f"{dbm:.0f} dBm ({wifi_map.quality(dbm)[0]}), {tx:.0f} Mbit/s, {where}"
    return {"best": describe(best), "worst": describe(worst), "avg": avg,
            "squares": len(cs), "weak_share": weak / len(cs)}


# -------------------------------------------------------------- options ---
PREFS = os.path.expanduser("~/.config/frame-settings/house.json")


def prefs():
    """{'minimap': show the mini-map in view, 'angle': the map's rotation in degrees}"""
    p = {"minimap": True, "angle": 0}
    try:
        with open(PREFS, encoding="utf-8") as f:
            p.update(json.load(f))
    except (OSError, ValueError):
        pass
    return p


def save_prefs(p):
    os.makedirs(os.path.dirname(PREFS), exist_ok=True)
    with open(PREFS, "w", encoding="utf-8") as f:
        json.dump(p, f)


# -------------------------------------------------------------- storage ---
def save(m):
    os.makedirs(HOUSE, exist_ok=True)
    tmp = CURRENT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(m, f)
    os.replace(tmp, CURRENT)
    return CURRENT


def load():
    """The current house map. The first time, an older single-walk save (house-*.json) is taken over."""
    try:
        with open(CURRENT, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        pass
    try:
        old = sorted(f for f in os.listdir(HOUSE) if f.startswith("house-") and f.endswith(".json"))
        with open(os.path.join(HOUSE, old[-1]), encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError, IndexError):
        return None
    m.setdefault("walks", 1)
    return m if m.get("samples") else None


def clear():
    """Start fresh: the current map (and any older saves) are kept as dated backups, then removed."""
    if not os.path.isdir(HOUSE):
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for f in os.listdir(HOUSE):
        if f == "current.json" or (f.startswith("house-") and f.endswith(".json")):
            os.replace(os.path.join(HOUSE, f), os.path.join(HOUSE, f"cleared-{stamp}-{f}"))


# ------------------------------------------------------------- mini-map ---
HEADER = 150  # mini-map: height of the big signal readout on top, pixels


def minimap_svg(m, pose, heading, target="wifi", reading=None, size=420, radius=5.0):
    """Game-style mini-map for the middle of your view, see-through. On top, the chosen link's signal
    in big numbers (reading: {"name", "dbm", "word", "colour", "other", "note"}); below, the map around
    the headset, turned so your view direction is up: walked squares coloured by `target` signal (grey =
    not measured), your recent path, labels and you as an arrow in the middle (about `radius` metres).
    SVG text, size x (HEADER + size) px."""
    samples = m.get("samples") or []
    c = size / 2
    k = (size / 2 - 16) / radius          # px per metre
    px, py, pz = pose
    fx, fz = heading
    rx, rz = -fz, fx                       # right of the view direction, on the floor

    def P(x, z):
        dx, dz = x - px, z - pz
        return c + (dx * rx + dz * rz) * k, c - (dx * fx + dz * fz) * k

    def pts(seq):
        return " ".join("%.1f,%.1f" % P(x, z) for x, z in seq)
    base = floor_base(samples) if samples else py
    floor = floor_of(py, base)
    reach = radius * 1.5
    r = reading or {}
    head = [f'<rect x="4" y="4" width="{size - 8}" height="{HEADER - 10}" rx="26" fill="#0e141b" fill-opacity="0.8" '
            f'stroke="#66ccff" stroke-opacity="0.8" stroke-width="4"/>']
    title = r.get("name", TARGETS[target]).upper() + (f" · {r['word']}" if r.get("dbm") is not None else "")
    head.append(f'<text x="{c:.1f}" y="44" fill="#c9d6e3" font-size="24" font-weight="bold" text-anchor="middle" '
                f'font-family="sans-serif">{title}</text>')
    big = r.get("note") or (f"{r['dbm']:.0f} dBm" if r.get("dbm") is not None else "no signal")
    head.append(f'<text x="{c:.1f}" y="{HEADER - 30}" fill="{r.get("colour", "#ffffff") if not r.get("note") else "#ffffff"}" '
                f'font-size="66" font-weight="bold" text-anchor="middle" font-family="sans-serif" stroke="#0e141b" '
                f'stroke-width="6" paint-order="stroke">{big}</text>')
    parts = [f'<defs><clipPath id="r"><rect x="4" y="4" width="{size - 8}" height="{size - 8}" rx="34"/>'
             f'</clipPath></defs>',
             f'<rect x="4" y="4" width="{size - 8}" height="{size - 8}" rx="34" fill="#0e141b" fill-opacity="0.6"/>',
             '<g clip-path="url(#r)">']
    measured = cells(samples, target)
    for f, i, j in footprint(samples):
        if f != floor:
            continue
        x0, z0 = i * CELL, j * CELL
        if math.hypot(x0 + CELL / 2 - px, z0 + CELL / 2 - pz) > reach:
            continue
        v = measured.get((f, i, j))
        colour = wifi_map.fill(v[0]) if v else "#2a3440"
        parts.append(f'<polygon points="{pts(((x0, z0), (x0 + CELL, z0), (x0 + CELL, z0 + CELL), (x0, z0 + CELL)))}" '
                     f'fill="{colour}" fill-opacity="0.8"/>')
    recent = [(s[0], s[2]) for s in samples[-240:] if floor_of(s[1], base) == floor
              and math.hypot(s[0] - px, s[2] - pz) < reach]
    if len(recent) > 1:
        parts.append(f'<polyline points="{pts(recent)}" fill="none" stroke="#ffffff" stroke-opacity="0.45" '
                     f'stroke-width="3" stroke-linejoin="round"/>')
    for lab in m.get("labels", []):
        if lab.get("floor", 0) == floor and math.hypot(lab["x"] - px, lab["z"] - pz) < reach:
            lx, ly = P(lab["x"], lab["z"])
            parts.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="8" fill="#66ccff" stroke="#0e141b" stroke-width="3"/>'
                         f'<text x="{lx:.1f}" y="{ly + 30:.1f}" fill="#ffffff" font-size="22" font-weight="bold" '
                         f'text-anchor="middle" font-family="sans-serif" stroke="#0e141b" stroke-width="5" '
                         f'paint-order="stroke">{html.escape(lab["name"])}</text>')
    parts.append("</g>")
    parts.append(f'<polygon points="{c:.1f},{c - 22:.1f} {c + 15:.1f},{c + 16:.1f} {c:.1f},{c + 8:.1f} '
                 f'{c - 15:.1f},{c + 16:.1f}" fill="#fde725" stroke="#0e141b" stroke-width="4" '
                 f'stroke-linejoin="round"/>')
    parts.append(f'<rect x="4" y="4" width="{size - 8}" height="{size - 8}" rx="34" fill="none" stroke="#66ccff" '
                 f'stroke-opacity="0.8" stroke-width="5"/>')
    parts.append(f'<line x1="{size - 30 - k:.1f}" y1="{size - 28}" x2="{size - 30}" y2="{size - 28}" stroke="#c9d6e3" '
                 f'stroke-width="5"/><text x="{size - 30 - k / 2:.1f}" y="{size - 38}" fill="#c9d6e3" font-size="20" '
                 f'text-anchor="middle" font-family="sans-serif">1 m</text>')
    if r.get("other"):  # the other link, small, bottom left
        parts.append(f'<text x="28" y="{size - 24}" fill="#c9d6e3" font-size="24" font-weight="bold" '
                     f'font-family="sans-serif" stroke="#0e141b" stroke-width="5" paint-order="stroke">'
                     f'{html.escape(r["other"])}</text>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{HEADER + size}">{"".join(head)}'
            f'<g transform="translate(0,{HEADER})">{"".join(parts)}</g></svg>')
