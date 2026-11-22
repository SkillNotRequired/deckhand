"""Whole-house map for Deckhand: the home's layout plus Wi-Fi and Steam-dongle signal.

Walk the house with the headset on (several walks add up into one map). Twice a second
a HouseRecorder stores where the headset is (space_tool track, standing space), the
strength of BOTH links, and wall points:
  home Wi-Fi    the Frame's link to your router (iw <station iface> link)
  Steam dongle  the PC dongle's link to the Frame's own 6 GHz access point
                (iw <ap iface> station dump: how loudly the Frame hears the dongle)
  walls         the Frame can't detect walls itself (its SteamVR has no plane/scene
                detection), so hold a controller against the wall as you walk: any
                controller held out more than WALL_REACH sideways from your head counts
                as a wall point. The edge of the walked area is the fallback outline.

Heights split floors. Results: a top-down map per floor (0.5 m squares coloured by the
chosen signal, walls, your path, labels) and a simple 3D model (.glb).
Tracking can jump if the Frame re-recognises a room; a jump over 2 m starts a new
path segment instead of drawing a line across the house.
"""
import json
import math
import os
import struct
import subprocess
import threading
import time

import wifi_map

HOUSE = os.path.expanduser("~/.local/share/frame-settings/wifi-house")
CURRENT = os.path.join(HOUSE, "current.json")
EXPORTS = os.path.expanduser("~/Documents/Deckhand")
CELL = 0.5
MAX_SECONDS = 3600
FLOOR_HEIGHT = 2.7   # metres between storeys (for splitting floors)
EYE_HEIGHT = 1.6     # head above its floor, roughly
WALL_HEIGHT = 2.4
WALL_REACH = 0.40    # without the trigger: arm raised and held this far out from the head ...
AUTO_HOLD = 5        # ... for this many pose updates (5 a second): about 1 s
LABELS = ["Router", "PC", "Bedroom", "Kitchen", "Living room", "Office", "Bathroom", "Garage", "Hall"]
TARGETS = wifi_map.TARGETS

# sample: [x, y, z, wifi_dbm, wifi_tx, segment, dongle_dbm, dongle_tx]
WIFI, WIFI_TX, SEG, DONGLE, DONGLE_TX = 3, 4, 5, 6, 7


ap_interface = wifi_map.ap_interface
dongle_signal = wifi_map.dongle_signal


class HouseRecorder:
    def __init__(self):
        self.samples, self.walls = [], []   # walls: [x, hand y, z, segment, head y, hand]
        self.pose = None                    # latest headset (x, y, z) or None
        self.heading = (0.0, -1.0)          # latest look direction on the floor (x, z)
        self.trace_walls = True             # record wall points (see _wall_check)
        self.hands = {}                     # "left"/"right" -> (x, y, z) when tracked
        self.buttons = {}                   # "left"/"right" -> (trigger 0..1, pressed-button mask)
        self.buttons_seen = False           # any button press ever came through (input reaches us)
        self._k_time = 0.0                  # when SteamVR last gave us controller buttons
        self.latest = None                  # latest home Wi-Fi reading
        self.latest_dongle = None
        self.running = False
        self.started = 0.0
        self.distance = 0.0
        self.segment = 0
        self.proc = None
        self.iface = self.ap = None
        self._last = None
        self._held = {}
        self._last_wall = {}

    def start(self, segment_offset=0):
        if not os.access(wifi_map.SPACE_TOOL, os.X_OK):
            raise RuntimeError("space_tool is missing: run build.sh on the Frame")
        self.iface, self.ap = wifi_map.station_interface(), ap_interface()
        self.segment = segment_offset
        self.proc = subprocess.Popen([wifi_map.SPACE_TOOL, "track", "200"], stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True)
        self.running, self.started = True, time.time()
        threading.Thread(target=self._read_poses, daemon=True).start()
        threading.Thread(target=self._sample, daemon=True).start()

    def _read_poses(self):
        for line in self.proc.stdout:
            p = line.split()
            if len(p) == 8 and p[0] == "p":
                pos = (float(p[3]), float(p[4]), float(p[5])) if p[2] == "1" else None
                if p[1] == "hmd":
                    self.pose = pos
                    fx, fz = float(p[6]), float(p[7])
                    if pos and math.hypot(fx, fz) > 0.2:
                        self.heading = (fx, fz)
                elif pos:
                    self.hands[p[1]] = pos
                    self._wall_check(p[1], pos)
                else:
                    self.hands.pop(p[1], None)
                    self._held[p[1]] = 0
            elif len(p) == 4 and p[0] == "k":
                try:
                    trig, mask = float(p[2]), int(p[3], 16)
                except ValueError:
                    continue
                self.buttons[p[1]] = (trig, mask)
                self._k_time = time.time()
                if trig > 0.05 or mask:
                    self.buttons_seen = True
            if not self.running:
                break

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

    def trigger_ok(self):
        """SteamVR is giving us controller buttons right now (not while the dashboard or a game has them)."""
        return time.time() - self._k_time < 2.0

    def pressed(self, side):
        """Trigger (or grip) held on that controller."""
        trig, mask = self.buttons.get(side, (0.0, 0))
        return trig > 0.5 or bool(mask & ((1 << 33) | (1 << 2)))

    def _wall_check(self, side, hand):
        """Five times a second per controller: is it marking a wall? Either the trigger/grip is held
        (instant), or the arm is raised and held out ≥ WALL_REACH from the head for about a second."""
        pose = self.pose
        if not (self.running and self.trace_walls and pose):
            self._held[side] = 0
            return
        if self.pressed(side):
            self._held[side] = max(self._held.get(side, 0), AUTO_HOLD) + 1
        else:
            out = (math.hypot(hand[0] - pose[0], hand[2] - pose[2]) > WALL_REACH
                   and pose[1] - 0.8 < hand[1] < pose[1] + 0.5)
            self._held[side] = self._held.get(side, 0) + 1 if out else 0
        if self._held[side] >= AUTO_HOLD:
            last = self._last_wall.get(side)
            if last is None or math.hypot(hand[0] - last[0], hand[2] - last[1]) > 0.08:
                self._last_wall[side] = (hand[0], hand[2])
                self.walls.append([round(hand[0], 2), round(hand[1], 2), round(hand[2], 2), self.segment,
                                   round(pose[1], 2), 0 if side == "left" else 1])

    def on_wall(self):
        """Hands being counted as on a wall right now: {"left"/"right": (x, y, z)}."""
        if not (self.running and self.trace_walls):
            return {}
        return {side: pos for side, pos in list(self.hands.items()) if self._held.get(side, 0) >= AUTO_HOLD}

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
        return self


def add_walk(m, rec):
    """Adds a finished walk to the house map `m` (or starts one)."""
    m = m or {"kind": "house", "samples": [], "walls": [], "labels": [], "distance": 0.0, "walks": 0}
    m["samples"] += rec.samples
    m["walls"] = m.get("walls", []) + rec.walls
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


def wall_lines(m, floor):
    """Traced walls on one floor as polylines [[(x, z), ...], ...], one per hand, broken at gaps > 0.6 m."""
    samples = m.get("samples") or []
    base = floor_base(samples) if samples else EYE_HEIGHT
    lines = []
    for hand in (0, 1):
        cur, last = [], None
        for w in m.get("walls", []):
            if (w[5] if len(w) > 5 else 0) != hand:
                continue
            x, z, seg = w[0], w[2], w[3]
            if floor_of(w[4] if len(w) > 4 else w[1] + 0.6, base) != floor:
                continue
            if last and (math.hypot(x - last[0], z - last[1]) > 0.6 or seg != last[2]):
                if len(cur) > 1:
                    lines.append(cur)
                cur = []
            cur.append((x, z))
            last = (x, z, seg)
        if len(cur) > 1:
            lines.append(cur)
    return lines


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
    """{'walls': trace walls with a held-out controller, 'minimap': show the mini-map in view}"""
    p = {"walls": True, "minimap": True}
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
    m.setdefault("walls", [])
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


# ------------------------------------------------------------ 3D model ---
def _srgb_to_linear(hex_colour):
    c = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    return [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in c]


def export_glb(m, target="wifi", path=None):
    """Simple 3D model: dark floor tiles where you walked, walls (traced ones solid, the walked
    area's outline see-through), a column per 0.5 m square (taller and greener = stronger
    signal for `target`), a light trail at head height, labels as tall poles. Y up, metres.
    One material per colour, so any viewer shows the colours (Windows 3D Viewer, Blender…)."""
    samples = m.get("samples", [])
    if not samples:
        raise RuntimeError("nothing to export: walk the house first")
    base = floor_base(samples)
    groups = {}  # material name -> (colour hex, alpha, [positions], [indices])

    def box(x0, y0, z0, x1, y1, z1, name, colour, alpha=1.0):
        _c, _a, pos, idx = groups.setdefault(name, (colour, alpha, [], []))
        start = len(pos)
        for x in (x0, x1):
            for y in (y0, y1):
                for z in (z0, z1):
                    pos.append((x, y, z))
        # corners: index = xi*4 + yi*2 + zi; faces wound outwards
        for a, b, c, d in ((0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)):
            idx.extend((start + a, start + b, start + c, start + a, start + c, start + d))

    def wall(xa, za, xb, zb, y0, name, colour, alpha, thick=0.04):
        """A vertical panel from (xa, za) to (xb, zb): a thin box turned along the segment."""
        _c, _a, pos, idx = groups.setdefault(name, (colour, alpha, [], []))
        dx, dz = xb - xa, zb - za
        ln = math.hypot(dx, dz)
        if ln < 1e-3:
            return
        nx, nz = -dz / ln * thick / 2, dx / ln * thick / 2
        start = len(pos)
        for (px, pz) in ((xa - nx, za - nz), (xa + nx, za + nz), (xb - nx, zb - nz), (xb + nx, zb + nz)):
            pos.append((px, y0, pz))
            pos.append((px, y0 + WALL_HEIGHT, pz))
        # quads: sides + ends + top (bottom never seen)
        for a, b, c, d in ((0, 4, 5, 1), (2, 3, 7, 6), (0, 1, 3, 2), (4, 6, 7, 5), (1, 5, 7, 3)):
            idx.extend((start + a, start + b, start + c, start + a, start + c, start + d))

    for f, i, j in footprint(samples):
        y0 = f * FLOOR_HEIGHT
        box(i * CELL, y0 - 0.03, j * CELL, (i + 1) * CELL, y0, (j + 1) * CELL, "floor (where you walked)", "#2a3340")
    for (f, i, j), (dbm, _tx, _n) in cells(samples, target).items():
        y0 = f * FLOOR_HEIGHT
        x0, z0 = i * CELL, j * CELL
        h = 0.1 + 1.2 * max(0.0, min(1.0, (dbm + 90) / 50))                      # -90 → 0.1 m, -40 → 1.3 m
        band = min(9, int(wifi_map.strength(dbm) * 10))         # 10 colour steps: one material each
        lo = wifi_map.DBM_LOW + band * (wifi_map.DBM_HIGH - wifi_map.DBM_LOW) / 10
        g = 0.07
        box(x0 + g, y0, z0 + g, x0 + CELL - g, y0 + h, z0 + CELL - g, f"{TARGETS[target]} {lo:.0f} dBm",
            wifi_map.fill(lo + 3))
    for f in floors(samples):
        y0 = f * FLOOR_HEIGHT
        for line in wall_lines(m, f):
            for (xa, za), (xb, zb) in zip(line, line[1:]):
                wall(xa, za, xb, zb, y0, "walls (traced)", "#c9d6e3", 1.0)
        for xa, za, xb, zb in outline_edges(samples, f):
            wall(xa, za, xb, zb, y0, "outline (edge of where you walked)", "#66ccff", 0.25, thick=0.02)
    last = None
    for s in samples:                                                            # trail every ~0.4 m
        x, y, z = s[0], s[1], s[2]
        if last is None or math.dist((x, y, z), last) > 0.4:
            fy = floor_of(y, base) * FLOOR_HEIGHT + EYE_HEIGHT
            box(x - 0.03, fy - 0.03, z - 0.03, x + 0.03, fy + 0.03, z + 0.03, "path", "#eef4fa")
            last = (x, y, z)
    for lab in m.get("labels", []):
        fy = lab.get("floor", 0) * FLOOR_HEIGHT
        box(lab["x"] - 0.04, fy, lab["z"] - 0.04, lab["x"] + 0.04, fy + 2.2, lab["z"] + 0.04,
            f"label {lab['name']}", "#66ccff")

    binary = b""
    views, accessors, materials, prims = [], [], [], []
    total_v = total_t = 0
    for name, (colour, alpha, pos, idx) in groups.items():
        if not pos:
            continue
        pos_b = b"".join(struct.pack("<3f", *p) for p in pos)
        idx_b = struct.pack(f"<{len(idx)}I", *idx)
        views.append({"buffer": 0, "byteOffset": len(binary), "byteLength": len(pos_b), "target": 34962})
        binary += pos_b
        views.append({"buffer": 0, "byteOffset": len(binary), "byteLength": len(idx_b), "target": 34963})
        binary += idx_b
        accessors.append({"bufferView": len(views) - 2, "componentType": 5126, "count": len(pos), "type": "VEC3",
                          "min": [min(p[k] for p in pos) for k in range(3)],
                          "max": [max(p[k] for p in pos) for k in range(3)]})
        accessors.append({"bufferView": len(views) - 1, "componentType": 5125, "count": len(idx), "type": "SCALAR"})
        mat = {"name": name, "doubleSided": True,
               "pbrMetallicRoughness": {"baseColorFactor": _srgb_to_linear(colour) + [alpha],
                                        "metallicFactor": 0, "roughnessFactor": 1}}
        if alpha < 1:
            mat["alphaMode"] = "BLEND"
        materials.append(mat)
        prims.append({"attributes": {"POSITION": len(accessors) - 2}, "indices": len(accessors) - 1,
                      "material": len(materials) - 1})
        total_v += len(pos)
        total_t += len(idx) // 3
    binary += b"\0" * (-len(binary) % 4)
    gltf = {
        "asset": {"version": "2.0", "generator": "Deckhand house map"},
        "scene": 0, "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": f"House map ({TARGETS[target]})"}],
        "meshes": [{"name": "house", "primitives": prims}],
        "materials": materials,
        "buffers": [{"byteLength": len(binary)}],
        "bufferViews": views,
        "accessors": accessors,
    }
    js = json.dumps(gltf, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    total = 12 + 8 + len(js) + 8 + len(binary)
    data = (struct.pack("<III", 0x46546C67, 2, total) + struct.pack("<II", len(js), 0x4E4F534A) + js
            + struct.pack("<II", len(binary), 0x004E4942) + binary)
    if not path:
        os.makedirs(EXPORTS, exist_ok=True)
        path = os.path.join(EXPORTS, time.strftime(f"house-{target}-%Y%m%d-%H%M.glb"))
    with open(path, "wb") as f:
        f.write(data)
    return path, total_v, total_t


# ------------------------------------------------------------- mini-map ---
def minimap_svg(m, pose, heading, target="wifi", caption="", size=420, radius=5.0, hands=None, on_wall=None,
                hint=False):
    """Game-style mini-map around the headset, turned so your view direction is up: walked
    squares coloured by `target` signal (grey = not measured), walls, recent path, labels and
    you as an arrow in the middle. SVG text, size x size px, showing about `radius` metres.
    hands: {"left"/"right": (x, y, z)} controller dots; on_wall: the hands on a wall right now
    (their dots turn cyan and the wall being traced glows)."""
    hands, on_wall = hands or {}, on_wall or {}
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
    parts = [f'<defs><clipPath id="r"><rect x="4" y="4" width="{size - 8}" height="{size - 8}" rx="34"/>'
             f'</clipPath></defs>',
             f'<rect x="4" y="4" width="{size - 8}" height="{size - 8}" rx="34" fill="#0e141b" fill-opacity="0.8"/>',
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
                     f'fill="{colour}" fill-opacity="0.85"/>')
    for xa, za, xb, zb in outline_edges(samples, floor):  # estimated walls: edge of the walked area
        if min(math.hypot(xa - px, za - pz), math.hypot(xb - px, zb - pz)) < reach:
            (ax, ay), (bx, by) = P(xa, za), P(xb, zb)
            parts.append(f'<line x1="{ax:.1f}" y1="{ay:.1f}" x2="{bx:.1f}" y2="{by:.1f}" stroke="#9fd8ff" '
                         f'stroke-width="3" stroke-dasharray="8 5"/>')
    recent = [(s[0], s[2]) for s in samples[-240:] if floor_of(s[1], base) == floor
              and math.hypot(s[0] - px, s[2] - pz) < reach]
    if len(recent) > 1:
        parts.append(f'<polyline points="{pts(recent)}" fill="none" stroke="#ffffff" stroke-opacity="0.45" '
                     f'stroke-width="3" stroke-linejoin="round"/>')
    for ln in wall_lines(m, floor):
        near = [p for p in ln if math.hypot(p[0] - px, p[1] - pz) < reach * 1.5]
        if len(near) > 1:
            parts.append(f'<polyline points="{pts(near)}" fill="none" stroke="#eef4fa" stroke-width="8" '
                         f'stroke-linecap="round" stroke-linejoin="round"/>')
            live = on_wall and any(math.hypot(near[-1][0] - h[0], near[-1][1] - h[2]) < 0.4 for h in on_wall.values())
            if live:  # the wall being traced right now: glow its newest 2 m
                tail, run = [near[-1]], 0.0
                for a in reversed(near[:-1]):
                    run += math.hypot(a[0] - tail[-1][0], a[1] - tail[-1][1])
                    tail.append(a)
                    if run > 2.0:
                        break
                parts.append(f'<polyline points="{pts(tail)}" fill="none" stroke="#66ccff" stroke-width="16" '
                             f'stroke-opacity="0.45" stroke-linecap="round" stroke-linejoin="round"/>'
                             f'<polyline points="{pts(tail)}" fill="none" stroke="#66ccff" stroke-width="8" '
                             f'stroke-linecap="round" stroke-linejoin="round"/>')
    for side, h in hands.items():  # controllers: grey dots, cyan with a ring when on a wall
        hx, hy = P(h[0], h[2])
        if side in on_wall:
            parts.append(f'<circle cx="{hx:.1f}" cy="{hy:.1f}" r="20" fill="none" stroke="#66ccff" stroke-width="4"/>'
                         f'<circle cx="{hx:.1f}" cy="{hy:.1f}" r="10" fill="#66ccff" stroke="#0e141b" stroke-width="3"/>')
        else:
            parts.append(f'<circle cx="{hx:.1f}" cy="{hy:.1f}" r="7" fill="#c9d6e3" fill-opacity="0.8" '
                         f'stroke="#0e141b" stroke-width="2"/>')
    for lab in m.get("labels", []):
        if lab.get("floor", 0) == floor and math.hypot(lab["x"] - px, lab["z"] - pz) < reach:
            lx, ly = P(lab["x"], lab["z"])
            parts.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="8" fill="#66ccff" stroke="#0e141b" stroke-width="3"/>'
                         f'<text x="{lx:.1f}" y="{ly + 30:.1f}" fill="#ffffff" font-size="22" font-weight="bold" '
                         f'text-anchor="middle" font-family="sans-serif" stroke="#0e141b" stroke-width="5" '
                         f'paint-order="stroke">{lab["name"]}</text>')
    parts.append("</g>")
    parts.append(f'<polygon points="{c:.1f},{c - 22:.1f} {c + 15:.1f},{c + 16:.1f} {c:.1f},{c + 8:.1f} '
                 f'{c - 15:.1f},{c + 16:.1f}" fill="#ffffff" stroke="#0e141b" stroke-width="4" '
                 f'stroke-linejoin="round"/>')
    parts.append(f'<rect x="4" y="4" width="{size - 8}" height="{size - 8}" rx="34" fill="none" stroke="#66ccff" '
                 f'stroke-opacity="0.8" stroke-width="5"/>')
    if caption:
        parts.append(f'<text x="{c:.1f}" y="44" fill="#ffffff" font-size="28" font-weight="bold" text-anchor="middle" '
                     f'font-family="sans-serif" stroke="#0e141b" stroke-width="6" paint-order="stroke">{caption}</text>')
    if on_wall:
        parts.append(f'<rect x="{c - 92:.1f}" y="{size - 70}" width="184" height="44" rx="22" fill="#66ccff"/>'
                     f'<text x="{c:.1f}" y="{size - 39}" fill="#0e141b" font-size="26" font-weight="bold" '
                     f'text-anchor="middle" font-family="sans-serif">Wall ✓</text>')
    elif hint:  # how to draw a wall: "trigger" when SteamVR passes us the trigger, else "arm"
        text = "Trigger on a wall: draw it" if hint == "trigger" else "Arm out on a wall: draw it"
        parts.append(f'<rect x="{c - 150:.1f}" y="{size - 66}" width="300" height="38" rx="19" fill="#0e141b" '
                     f'fill-opacity="0.85"/><text x="{c:.1f}" y="{size - 40}" fill="#c9d6e3" font-size="21" '
                     f'text-anchor="middle" font-family="sans-serif">{text}</text>')
    parts.append(f'<line x1="{size - 30 - k:.1f}" y1="{size - 28}" x2="{size - 30}" y2="{size - 28}" stroke="#c9d6e3" '
                 f'stroke-width="5"/><text x="{size - 30 - k / 2:.1f}" y="{size - 38}" fill="#c9d6e3" font-size="20" '
                 f'text-anchor="middle" font-family="sans-serif">1 m</text>')
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}">{"".join(parts)}</svg>'
