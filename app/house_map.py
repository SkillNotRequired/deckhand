"""Whole-house map for Deckhand: the home's layout plus Wi-Fi and Steam-dongle signal.

Walk the house with the headset on (several walks add up into one map). Twice a second
a HouseRecorder stores where the headset is (space_tool track, standing space) and the
strength of BOTH links:
  home Wi-Fi    the Frame's link to your router (iw <station iface> link)
  Steam dongle  the PC dongle's link to the Frame's own 6 GHz access point
                (iw <ap iface> station dump: how loudly the Frame hears the dongle)
Every position is kept, measured or not, so dead zones still show as walked floor.

Heights split floors. Results: a top-down map per floor (0.5 m squares coloured by the
chosen signal, the edge of where you walked, your path, labels).
Tracking can jump if the Frame re-recognises a room; a jump over 2 m starts a new
path segment instead of drawing a line across the house.

A walk in progress is saved every 30 s (walk-inprogress-<pid>.json); if Deckhand closes
or crashes mid-walk, recover() adds it to the map the next time.
"""
import html
import json
import math
import os
import threading
import time

import wifi_map

HOUSE = os.path.expanduser("~/.local/share/frame-settings/wifi-house")
CURRENT = os.path.join(HOUSE, "current.json")
CELL = 0.5
MAX_SECONDS = 3600
AUTOSAVE_EVERY = 30  # seconds
FLOOR_HEIGHT = 2.7   # metres between storeys (for splitting floors)
EYE_HEIGHT = 1.6     # head above its floor, roughly
LABELS = ["Router", "PC", "Bedroom", "Kitchen", "Living room", "Office", "Bathroom", "Garage", "Hall"]
TARGETS = wifi_map.TARGETS
KEEP_CLEARED = 3     # Clear map keeps this many earlier maps as backups

# sample: [x, y, z, wifi_dbm, wifi_tx, segment, dongle_dbm, dongle_tx] (None = not measured)
WIFI, WIFI_TX, SEG, DONGLE, DONGLE_TX = 3, 4, 5, 6, 7

ap_interface = wifi_map.ap_interface
dongle_signal = wifi_map.dongle_signal
PoseWatcher = wifi_map.PoseWatcher  # the "You" marker while the map page is open


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
        self._sampler = None
        self._save_lock = threading.Lock()

    def start(self, segment_offset=0):
        """Looks up the radio interfaces (subprocesses): call it off the UI thread."""
        self.iface, self.ap = wifi_map.station_interface(), ap_interface()
        self.segment = segment_offset
        self.samples, self.distance, self._last = [], 0.0, None
        super().start()
        self.started = time.monotonic()
        self._sampler = threading.Thread(target=self._sample, daemon=True)
        self._sampler.start()
        return self

    def _sample(self):
        saved_at = ap_checked = time.monotonic()
        while self.running:
            if self.elapsed() > MAX_SECONDS:
                self.running = False  # the app sees this and finishes the walk
                break
            if not self.ap and time.monotonic() - ap_checked > 10:  # the dongle's link may come up later
                ap_checked = time.monotonic()
                self.ap = ap_interface()
            s = wifi_map.signal(self.iface)
            d = dongle_signal(self.ap) if self.ap else None
            self.latest, self.latest_dongle = s, d
            s = s if s and s["dbm"] is not None else None
            pose = self.pose
            if pose:
                if self._last:
                    if math.dist(pose, self._last) > 2.0:
                        self.segment += 1  # tracking jumped: don't join it up
                    else:
                        self.distance += math.hypot(pose[0] - self._last[0], pose[2] - self._last[2])
                self._last = pose
                self.samples.append([round(pose[0], 2), round(pose[1], 2), round(pose[2], 2),
                                     s["dbm"] if s else None, s["tx"] if s else None, self.segment,
                                     d["dbm"] if d else None, d["tx"] if d else None])
            if self.samples and time.monotonic() - saved_at > AUTOSAVE_EVERY:
                saved_at = time.monotonic()
                self.autosave()
            self._wake.wait(0.5)

    def autosave(self):
        """Keeps the walk so far on disk (recover() adds it to the map if Deckhand doesn't finish it)."""
        with self._save_lock:
            if not self.running:  # stopped meanwhile: the finished walk is being saved instead
                return
            try:
                write_json(inprogress_path(), {"samples": list(self.samples), "distance": round(self.distance, 1),
                                               "ssid": (self.latest or {}).get("ssid", ""), "saved": time.time()})
            except OSError:
                pass

    def elapsed(self):
        return time.monotonic() - self.started if self.started else 0

    def stop(self):
        super().stop()
        with self._save_lock:
            pass  # an autosave already writing finishes first; later ones see running == False
        if self._sampler and self._sampler is not threading.current_thread():
            self._sampler.join(timeout=1)  # its last reading lands before the walk is added
        return self


def new_map():
    return {"kind": "house", "samples": [], "labels": [], "distance": 0.0, "walks": 0}


def merge_walk(m, samples, distance=0.0, ssid=None):
    """A new map dict: `m` (or a new map) plus one walk. `m` itself is not changed."""
    m = dict(m) if m else new_map()
    m["samples"] = list(m.get("samples") or []) + list(samples)
    m["labels"] = m.get("labels") or []
    m["distance"] = round((m.get("distance") or 0) + (distance or 0), 1)
    m["walks"] = (m.get("walks") or 0) + 1
    m["saved"] = time.time()
    m["ssid"] = ssid or m.get("ssid", "")
    return m


def add_walk(m, rec):
    """The house map `m` (or a new one) with the recorder's walk added (a new dict)."""
    return merge_walk(m, rec.samples, rec.distance, (rec.latest or {}).get("ssid"))


def finish_walk(m, rec):
    """Adds a finished walk to the map and saves it; the walk's autosave is no longer needed.
    Returns the new map (`m` unchanged if the walk has no positions)."""
    if rec.samples:
        m = add_walk(m, rec)
        save(m)
    discard_inprogress()
    return m


def inprogress_path(pid=None):
    """This app's walk-in-progress file (one per running Deckhand, so a second window never takes
    over a walk that is still going)."""
    return os.path.join(HOUSE, f"walk-inprogress-{pid or os.getpid()}.json")


def discard_inprogress():
    try:
        os.remove(inprogress_path())
    except OSError:
        pass


def _deckhand_running(pid):
    if pid == os.getpid():
        return False  # left by an earlier run with our number: nothing of ours is walking yet
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return b"deckhand" in f.read()
    except OSError:
        return False


def recover():
    """Adds walks that were cut short (Deckhand closed or crashed mid-walk) to the saved map.
    Returns how many positions came back (0: nothing to recover). Call once at start-up,
    before a walk starts."""
    try:
        names = [f for f in os.listdir(HOUSE) if f.startswith("walk-inprogress-") and f.endswith(".json")]
    except OSError:
        return 0
    total = 0
    for name in sorted(names):
        try:
            pid = int(name[len("walk-inprogress-"):-len(".json")])
        except ValueError:
            pid = 0
        if pid and _deckhand_running(pid):
            continue  # another Deckhand window is walking right now
        path = os.path.join(HOUSE, name)
        try:
            with open(path, encoding="utf-8") as f:
                w = json.load(f)
        except (OSError, ValueError):
            w = None
        samples = clean_samples(w.get("samples")) if isinstance(w, dict) else []
        if samples:
            m = load()
            segs = [smp[SEG] for smp in (m or {}).get("samples", [])]
            shift = (max(segs) + 1 if segs else 0) - min(smp[SEG] for smp in samples)
            samples = [smp[:SEG] + [smp[SEG] + shift] + smp[SEG + 1:] for smp in samples]  # its own paths
            save(merge_walk(m, samples, w.get("distance") or 0, w.get("ssid")))
            total += len(samples)
        try:
            os.remove(path)
        except OSError:
            pass
    return total


# ------------------------------------------------------------- analysis ---
def clean_samples(samples):
    """Samples with a real position (drops damaged entries rather than failing on them)."""
    out = []
    for s in samples or []:
        try:
            if len(s) > SEG and all(math.isfinite(s[i]) for i in (0, 1, 2)):
                out.append(s)
        except TypeError:
            continue
    return out


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


def cells(samples, target="wifi", base=None):
    """{(floor, i, j): (avg dBm, avg Mbit/s, count)} for the chosen signal. `base`: the map's
    floor_base() when `samples` is only part of the map."""
    base = floor_base(samples) if base is None else base
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


def footprint(samples, base=None):
    """{(floor, i, j)} squares walked (whatever was measured there)."""
    base = floor_base(samples) if base is None else base
    return {(floor_of(s[1], base), int(s[0] // CELL), int(s[2] // CELL)) for s in samples}


def outline_edges(samples, floor):
    """Edges of the walked area on one floor (where a walked square meets an unwalked one): the
    outline of the rooms. [(x0, z0, x1, z1)]"""
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
    p = {"minimap": True, "angle": 0.0}
    try:
        with open(PREFS, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            p.update({k: d[k] for k in p if k in d})
    except (OSError, ValueError):
        pass
    try:
        p["angle"] = float(p["angle"]) % 360
    except (TypeError, ValueError):
        p["angle"] = 0.0
    p["minimap"] = bool(p["minimap"])
    return p


def save_prefs(p):
    try:
        write_json(PREFS, p)
    except OSError:
        pass  # a view preference isn't worth an error message


# -------------------------------------------------------------- storage ---
def write_json(path, data):
    """Writes the whole file or nothing (a crash or power loss never leaves half a file)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    return path


def save(m):
    return write_json(CURRENT, m)


def _read_map(path):
    try:
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        if path == CURRENT:  # unreadable: set it aside instead of overwriting it on the next save
            try:
                os.replace(path, path + time.strftime(".damaged-%Y%m%d-%H%M%S"))
            except OSError:
                pass
        return None
    return m if isinstance(m, dict) else None


def load():
    """The current house map, or None. The first time, an older single-walk save (house-*.json) is
    taken over."""
    m = _read_map(CURRENT)
    if m is None:
        try:
            old = sorted(f for f in os.listdir(HOUSE) if f.startswith("house-") and f.endswith(".json"))
        except OSError:
            old = []
        for name in reversed(old):
            m = _read_map(os.path.join(HOUSE, name))
            if m and m.get("samples"):
                m.setdefault("walks", 1)
                break
            m = None
    if m is None:
        return None
    m["samples"] = clean_samples(m.get("samples"))
    m["labels"] = [lab for lab in (m.get("labels") or []) if isinstance(lab, dict)
                   and {"name", "x", "z"} <= lab.keys()]
    return m


def clear():
    """Start fresh: the current map (and any older saves) are kept as dated backups (the newest
    three Clear presses), then removed."""
    discard_inprogress()
    if not os.path.isdir(HOUSE):
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for f in os.listdir(HOUSE):
        if f == "current.json" or (f.startswith("house-") and f.endswith(".json")):
            os.replace(os.path.join(HOUSE, f), os.path.join(HOUSE, f"cleared-{stamp}-{f}"))
    backups = [f for f in os.listdir(HOUSE) if f.startswith("cleared-")]
    keep = sorted({f.split("-")[1] + f.split("-")[2] for f in backups if f.count("-") >= 3})[-KEEP_CLEARED:]
    for f in backups:
        if f.count("-") >= 3 and f.split("-")[1] + f.split("-")[2] not in keep:
            try:
                os.remove(os.path.join(HOUSE, f))
            except OSError:
                pass


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
                f'font-family="sans-serif">{html.escape(title)}</text>')
    big = r.get("note") or (f"{r['dbm']:.0f} dBm" if r.get("dbm") is not None else "no signal")
    big_colour = "#ffffff" if r.get("note") else r.get("colour", "#ffffff")
    head.append(f'<text x="{c:.1f}" y="{HEADER - 30}" fill="{big_colour}" '
                f'font-size="66" font-weight="bold" text-anchor="middle" font-family="sans-serif" stroke="#0e141b" '
                f'stroke-width="6" paint-order="stroke">{html.escape(big)}</text>')
    parts = [f'<defs><clipPath id="r"><rect x="4" y="4" width="{size - 8}" height="{size - 8}" rx="34"/>'
             f'</clipPath></defs>',
             f'<rect x="4" y="4" width="{size - 8}" height="{size - 8}" rx="34" fill="#0e141b" fill-opacity="0.6"/>',
             '<g clip-path="url(#r)">']
    # Only the squares around you matter: a quick box test first keeps this light on a big map.
    near = [s for s in samples if abs(s[0] - px) <= reach + CELL and abs(s[2] - pz) <= reach + CELL]
    measured = cells(near, target, base)
    for f, i, j in footprint(near, base):
        if f != floor:
            continue
        x0, z0 = i * CELL, j * CELL
        if math.hypot(x0 + CELL / 2 - px, z0 + CELL / 2 - pz) > reach:
            continue
        v = measured.get((f, i, j))
        colour = wifi_map.fill(v[0]) if v else "#2a3440"
        parts.append(f'<polygon points="{pts(((x0, z0), (x0 + CELL, z0), (x0 + CELL, z0 + CELL), (x0, z0 + CELL)))}" '
                     f'fill="{colour}" fill-opacity="0.8"/>')
    runs, run, seg = [], [], None  # your recent path: broken where tracking jumped or it leaves the view
    for s in samples[-240:]:
        keep = floor_of(s[1], base) == floor and math.hypot(s[0] - px, s[2] - pz) < reach
        if not keep or s[SEG] != seg:
            if len(run) > 1:
                runs.append(run)
            run = []
        seg = s[SEG]
        if keep:
            run.append((s[0], s[2]))
    if len(run) > 1:
        runs.append(run)
    for run in runs:
        parts.append(f'<polyline points="{pts(run)}" fill="none" stroke="#ffffff" stroke-opacity="0.45" '
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
