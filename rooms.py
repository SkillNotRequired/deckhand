#!/usr/bin/env python3
"""Saved rooms (play areas) for the Steam Frame.

How the Frame remembers a room: its tracking service (XRService, started by
SteamVR) keeps one map of your surroundings (cv/xrservice/serializedmap/*.pb),
and SteamVR keeps the boundaries drawn in it (chaperone_info.vrchap; it can hold
several). When the Frame recognises the map it restores the boundary that was
in use when the map was saved ("Relocalized: universeId ..." in the log).
Setting up somewhere else, or a failed match, starts a new map.

A saved room is a copy of the map + boundaries, with a name and a top-down
outline of the boundary in use (for the preview). Loading one swaps them back
in while SteamVR is stopped (XRService writes its map on exit, so the swap has
to happen after it quits), then starts SteamVR so the Frame relocalises into
the saved map and restores that boundary. The room in use is backed up first
("Undo last load"). Tested 2026-11-22: restored 2 s after SteamVR came back.

    rooms.py --apply <room_id>   (run detached by the app: stop SteamVR, swap, start)
    rooms.py --list
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

CFG = os.path.expanduser("~/.config/openvr/config")
VRCHAP = os.path.join(CFG, "chaperone_info.vrchap")
MAP_DIR = os.path.join(CFG, "cv", "xrservice", "serializedmap")
ROOMS = os.path.expanduser("~/.local/share/frame-settings/rooms")
BACKUP = "_before_last_load"
LOG = os.path.join(ROOMS, "rooms.log")
SUGGESTED_NAMES = ["Bedroom", "Living room", "Office", "Kitchen", "Garage"]
DUMMY_UNIVERSES = {"424242", "525252"}  # the Frame's stand-in boundaries while it isn't tracking a room


def log(msg):
    os.makedirs(ROOMS, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


# ------------------------------------------------------------ geometry ---
def _floor_outline(universe):
    """Boundary as floor points [(x, z), ...] from the wall quads' floor corners."""
    pts = []
    for quad in universe.get("collision_bounds") or []:
        floor = [p for p in quad if abs(p[1]) < 0.05]
        if floor:
            pts.append((round(floor[0][0], 3), round(floor[0][2], 3)))
    return pts


def _area(pts):
    if len(pts) < 3:
        return 0.0
    s = sum(x1 * z2 - x2 * z1 for (x1, z1), (x2, z2) in zip(pts, pts[1:] + pts[:1]))
    return abs(s) / 2


def active_universe_id():
    """The boundary in use: the last universe the tracking service relocalised into,
    restored, or created this boot (None if the log has none)."""
    r = subprocess.run(["journalctl", "-b", "-o", "cat", "--no-pager", "-g",
                        "Relocalized: universeId|Created and Switched to a chaperone universe|"
                        "Restored chaperone universe"], capture_output=True, text=True)
    ids = [i for i in re.findall(r"(?:universeId|universe)\s+(\d{6,})", r.stdout)
           if i not in DUMMY_UNIVERSES]  # placeholders used while tracking is lost or in standby
    return ids[-1] if ids else None


def _pick_universe(unis, prefer_id=None):
    if prefer_id:
        # the tracking service's log line cuts long ids short, so a prefix match counts
        p = str(prefer_id)
        for u in unis:
            uid = str(u.get("universeID"))
            if uid == p or (len(p) >= 12 and uid.startswith(p)):
                return u
    drawn = [u for u in unis if u.get("collision_bounds")]
    pool = drawn or unis
    # newest boundary ("time" is written when it was drawn/changed)
    def when(u):
        try:
            return time.mktime(time.strptime(u.get("time", ""), "%a %b %d %H:%M:%S %Y"))
        except ValueError:
            return 0
    return max(pool, key=when) if pool else None


def describe(vrchap_path, map_dir, prefer_universe=None):
    """Facts + outline of a room's boundary in use (for the card and its preview)."""
    info = {"area": 0.0, "walls": 0, "map_mb": 0.0, "maps": [], "universe": None,
            "outline": [], "standing": None, "boundaries": 0}
    try:
        with open(vrchap_path, encoding="utf-8") as f:
            unis = json.load(f).get("universes", [])
        info["boundaries"] = sum(1 for u in unis if u.get("collision_bounds"))
        u = _pick_universe(unis, prefer_universe)
        if u:
            pts = _floor_outline(u)
            info.update(universe=str(u.get("universeID")), outline=pts, area=round(_area(pts), 1),
                        walls=len(u.get("collision_bounds") or []))
            st = u.get("standing") or {}
            if st.get("translation"):
                t = st["translation"]
                info["standing"] = [round(t[0], 3), round(t[2], 3), round(st.get("yaw", 0.0), 4)]
    except (OSError, ValueError):
        pass
    try:
        maps = [m for m in os.listdir(map_dir) if m.endswith(".pb")]
        info["maps"] = maps
        info["map_mb"] = round(sum(os.path.getsize(os.path.join(map_dir, m)) for m in maps) / 1e6, 1)
    except OSError:
        pass
    return info


def current():
    return describe(VRCHAP, MAP_DIR, active_universe_id())


# --------------------------------------------------------------- rooms ---
def room_dir(room_id):
    return os.path.join(ROOMS, room_id.replace("/", "_"))


def list_rooms():
    """Saved rooms, newest first: [{'id', 'name', 'saved', ...describe()}] (backup excluded)."""
    out = []
    try:
        names = os.listdir(ROOMS)
    except OSError:
        return out
    for rid in names:
        if rid == BACKUP or rid.endswith(".tmp"):
            continue
        meta = os.path.join(ROOMS, rid, "meta.json")
        try:
            with open(meta, encoding="utf-8") as f:
                m = json.load(f)
        except (OSError, ValueError):
            continue
        if not m.get("outline"):  # saved by an older version: fill in the preview data once
            m.update(describe(os.path.join(ROOMS, rid, "chaperone_info.vrchap"),
                              os.path.join(ROOMS, rid, "serializedmap")))
            _write_meta(rid, m)
        m["id"] = rid
        out.append(m)
    return sorted(out, key=lambda m: -m.get("saved", 0))


def backup_exists():
    return os.path.isfile(os.path.join(room_dir(BACKUP), "meta.json"))


def _write_meta(rid, meta):
    with open(os.path.join(room_dir(rid), "meta.json"), "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in meta.items() if k != "id"}, f, indent=2)


def _copy_room(dest):
    """Copies the live map + boundaries into dest (atomically, retrying if the map changes mid-copy)."""
    tmp = dest + ".tmp"
    for attempt in range(3):
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(os.path.join(tmp, "serializedmap"))
        before = {m: os.stat(os.path.join(MAP_DIR, m)).st_mtime_ns for m in os.listdir(MAP_DIR)}
        for m in before:
            shutil.copy2(os.path.join(MAP_DIR, m), os.path.join(tmp, "serializedmap", m))
        shutil.copy2(VRCHAP, os.path.join(tmp, "chaperone_info.vrchap"))
        after = {m: os.stat(os.path.join(MAP_DIR, m)).st_mtime_ns for m in os.listdir(MAP_DIR)}
        if before == after:
            break
        log(f"map changed while copying (attempt {attempt + 1}); retrying")
    else:
        raise RuntimeError("the room map kept changing while saving; try again in a moment")
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(tmp, dest)


def find_by_name(name):
    for m in list_rooms():
        if m.get("name", "").strip().lower() == name.strip().lower():
            return m["id"]
    return None


def save(name, room_id=None):
    """Saves the room in use as `name` (replacing a room with that name, or room_id)."""
    name = name.strip()[:40] or "Room"
    if not os.path.isfile(VRCHAP) or not os.path.isdir(MAP_DIR):
        raise RuntimeError("no room to save yet (no map or boundary on this headset)")
    rid = room_id or find_by_name(name) or (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "room") + \
        time.strftime("-%Y%m%d-%H%M%S")
    universe = active_universe_id()
    _copy_room(room_dir(rid))
    meta = {"name": name, "saved": time.time(),
            **describe(os.path.join(room_dir(rid), "chaperone_info.vrchap"),
                       os.path.join(room_dir(rid), "serializedmap"), universe)}
    _write_meta(rid, meta)
    log(f"saved '{name}' ({rid}): boundary {meta['universe']}, {meta['area']} m², map {meta['maps']}")
    return rid, meta


def rename(room_id, name):
    meta_path = os.path.join(room_dir(room_id), "meta.json")
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    meta["name"] = name.strip()[:40] or meta.get("name", "Room")
    _write_meta(room_id, meta)
    log(f"renamed {room_id} to '{meta['name']}'")


def delete(room_id):
    shutil.rmtree(room_dir(room_id), ignore_errors=True)
    log(f"deleted {room_id}")


LAST_LOAD = os.path.join(ROOMS, ".last_load")
LOAD_COOLDOWN = 60  # SteamVR stops auto-restarting tracking after several restarts within a minute


def start_load(room_id, quick=False):
    """Runs the load as its own background job (a full load stops SteamVR, which closes the app).

    Default is the full load (restart SteamVR, ~12 s): the user preferred it to the quick
    load (tracking-only restart, ~19 s of frozen/black view, apps stay open), which is kept
    for reference but not used by the app."""
    if not os.path.isfile(os.path.join(room_dir(room_id), "meta.json")):
        return False, "that room isn't saved"
    if quick:
        try:
            wait = LOAD_COOLDOWN - (time.time() - os.path.getmtime(LAST_LOAD))
        except OSError:
            wait = 0
        if wait > 0:
            return False, f"give tracking a minute between loads (about {int(wait) + 1} s more)"
        os.makedirs(ROOMS, exist_ok=True)
        with open(LAST_LOAD, "w") as f:
            f.write(room_id)
    subprocess.run(["systemctl", "--user", "reset-failed", "frame-room-load"], capture_output=True)
    r = subprocess.run(["systemd-run", "--user", "--quiet", "--collect", "--unit=frame-room-load",
                        "--description=Deckhand: load a saved room",
                        sys.executable, os.path.abspath(__file__), "--quick" if quick else "--apply", room_id],
                       capture_output=True, text=True)
    return r.returncode == 0, (r.stdout + r.stderr).strip() or "loading"


XRSERVICE_EXE = "/drivers/cv/bin/linuxarm64/XRService"  # its process name is "XRServiceLoopTh", so match the program


def _pid(name):
    """Newest process running `name` (vrserver) or the tracking service ("XRService"), by program file."""
    suffix = XRSERVICE_EXE if name == "XRService" else f"/bin/linuxarm64/{name}"
    best = None
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            if os.readlink(f"/proc/{d}/exe").endswith(suffix):
                best = max(best or 0, int(d))
        except OSError:
            continue
    return best


def _merge_universes(src_vrchap):
    """Adds the saved room's boundaries to SteamVR's list (keeps the ones already there)."""
    try:
        with open(VRCHAP, encoding="utf-8") as f:
            live = json.load(f)
        with open(src_vrchap, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return 0
    have = {str(u.get("universeID")) for u in live.get("universes", [])}
    add = [u for u in saved.get("universes", []) if str(u.get("universeID")) not in have]
    if add:
        live.setdefault("universes", []).extend(add)
        tmp = VRCHAP + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(live, f, indent=3)
        os.replace(tmp, VRCHAP)
    return len(add)


def quick_apply(room_id):
    """Loads a room by restarting only the tracking service (XRService), not SteamVR.

    The boundary travels inside the room map: on recognising a map, XRService hands
    SteamVR the boundary's id and outline, and SteamVR re-creates it if needed. So:
    freeze XRService (it can't write its map mid-swap), back up + swap the map, add
    the saved boundaries to SteamVR's list, then kill it. SteamVR's watchdog restarts
    a tracking service that didn't exit cleanly, and it relocalises into the swapped
    map. Apps keep running; tracking pauses for a few seconds. Falls back to a full
    SteamVR restart if tracking doesn't come back.
    """
    src = room_dir(room_id)
    tool = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chaperone_tool")
    pid = _pid("XRService")
    if not pid or not _pid("vrserver"):
        log(f"quick load {room_id}: SteamVR/tracking not running; doing a full load")
        return apply(room_id)
    universe_before = active_universe_id()
    t0 = time.time()
    log(f"quick load {room_id}: freezing tracking service {pid}")
    os.kill(pid, signal.SIGSTOP)
    swapped = False
    try:
        if room_id != BACKUP:
            _copy_room(room_dir(BACKUP))  # what was in use, for "Undo last load"
            _write_meta(BACKUP, {"name": "Before last load", "saved": time.time(),
                                 **describe(VRCHAP, MAP_DIR, universe_before)})
        for m in os.listdir(MAP_DIR):  # map files (and any half-written temp file)
            os.remove(os.path.join(MAP_DIR, m))
        for m in os.listdir(os.path.join(src, "serializedmap")):
            shutil.copy2(os.path.join(src, "serializedmap", m), os.path.join(MAP_DIR, m))
        added = _merge_universes(os.path.join(src, "chaperone_info.vrchap"))
        # SteamVR keeps boundaries in memory and writes them back over the file: make it reload
        # the merged file now, so the room's boundary is there when tracking recognises the room.
        r = subprocess.run([tool, "reload"], capture_output=True, text=True, timeout=15)
        swapped = True
        log(f"quick load {room_id}: map swapped, {added} boundaries added, "
            f"SteamVR reload: {(r.stdout + r.stderr).strip() or r.returncode}")
    except Exception as e:  # noqa: BLE001
        log(f"quick load {room_id}: swap failed: {e}")
    finally:
        os.kill(pid, signal.SIGKILL)  # no clean exit = no map save over ours; the watchdog restarts it
    new_pid = None
    for _ in range(60):
        time.sleep(0.5)
        p = _pid("XRService")
        if p and p != pid:
            new_pid = p
            break
    if not new_pid:
        log(f"quick load {room_id}: tracking service didn't come back in 30 s; restarting SteamVR")
        subprocess.run(["systemctl", "--user", "restart", "steamvr.service"], capture_output=True)
        return 1
    log(f"quick load {room_id}: tracking service back as {new_pid} after {time.time() - t0:.1f} s"
        + ("" if swapped else " (swap failed: old map in use)"))
    target = _saved_active_universe(src)
    if not swapped or not target:
        return 0
    # Wait for the room to be recognised: SteamVR then has that boundary (often re-created
    # as a plain square, because its in-memory list dropped the saved shape).
    for _ in range(60):
        time.sleep(0.5)
        u = active_universe_id()
        if u and target.startswith(u):
            break
    else:
        log(f"quick load {room_id}: room not recognised within 30 s (look around the room; "
            "the boundary comes back once it is)")
        return 0
    time.sleep(1.5)  # let SteamVR finish writing its own copy first
    fixed = _restore_shape(src, target, tool)
    log(f"quick load {room_id}: recognised (boundary {target}), shape "
        f"{'restored' if fixed else 'already right'}; {time.time() - t0:.1f} s total")
    return 0


def _saved_active_universe(src):
    """Full id of the boundary that was in use when the room was saved (from its meta)."""
    try:
        with open(os.path.join(src, "meta.json"), encoding="utf-8") as f:
            return str(json.load(f).get("universe") or "") or None
    except (OSError, ValueError):
        return None


def _restore_shape(src, target, tool):
    """Puts the saved shape (walls, play area, standing/seated origins) back on boundary `target`
    in SteamVR's file and makes SteamVR reload it. Returns True if it changed anything."""
    try:
        with open(os.path.join(src, "chaperone_info.vrchap"), encoding="utf-8") as f:
            saved = next(u for u in json.load(f)["universes"] if str(u.get("universeID")) == target)
        with open(VRCHAP, encoding="utf-8") as f:
            live = json.load(f)
    except (OSError, ValueError, StopIteration, KeyError):
        return False
    unis = live.setdefault("universes", [])
    cur = next((u for u in unis if str(u.get("universeID")) == target), None)
    if cur and len(cur.get("collision_bounds") or []) == len(saved.get("collision_bounds") or []):
        return False
    if cur:
        for k in ("collision_bounds", "play_area", "standing", "seated"):
            if k in saved:
                cur[k] = saved[k]
    else:
        unis.append(saved)
    tmp = VRCHAP + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(live, f, indent=3)
    os.replace(tmp, VRCHAP)
    subprocess.run([tool, "reload"], capture_output=True, text=True, timeout=15)
    return True


def _wait_gone(names, timeout=40):
    for _ in range(timeout * 2):
        if not any(_pid(n) for n in names):
            return True
        time.sleep(0.5)
    return False


def apply(room_id):
    src = room_dir(room_id)
    log(f"load {room_id}: stopping SteamVR")
    universe_before = active_universe_id()
    subprocess.run(["systemctl", "--user", "stop", "steamvr.service"], capture_output=True)
    if not _wait_gone(["vrserver", "XRService"]):
        log("SteamVR did not stop in time; nothing swapped, starting it again")
        subprocess.run(["systemctl", "--user", "start", "steamvr.service"], capture_output=True)
        return 1
    try:
        if room_id != BACKUP:
            _copy_room(room_dir(BACKUP))  # what was in use, for "Undo last load"
            _write_meta(BACKUP, {"name": "Before last load", "saved": time.time(),
                                 **describe(VRCHAP, MAP_DIR, universe_before)})
        for m in os.listdir(MAP_DIR):
            if m.endswith(".pb"):
                os.remove(os.path.join(MAP_DIR, m))
        for m in os.listdir(os.path.join(src, "serializedmap")):
            shutil.copy2(os.path.join(src, "serializedmap", m), os.path.join(MAP_DIR, m))
        shutil.copy2(os.path.join(src, "chaperone_info.vrchap"), VRCHAP)
        log(f"load {room_id}: swapped in")
    except Exception as e:  # noqa: BLE001
        log(f"load {room_id} failed while swapping: {e}")
    finally:
        subprocess.run(["systemctl", "--user", "start", "steamvr.service"], capture_output=True)
        log("SteamVR started")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--apply"] and len(sys.argv) == 3:
        sys.exit(apply(sys.argv[2]))
    elif sys.argv[1:2] == ["--quick"] and len(sys.argv) == 3:
        sys.exit(quick_apply(sys.argv[2]))
    elif sys.argv[1:] == ["--list"]:
        print(json.dumps({"current": current(), "saved": list_rooms()}, indent=2)[:4000])
    else:
        print(__doc__)
