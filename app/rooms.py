#!/usr/bin/env python3
"""Saved rooms (play areas) for the Steam Frame.

How the Frame remembers a room: its tracking service (XRService, started by
SteamVR) keeps one map of your surroundings (cv/xrservice/serializedmap/*.pb),
and SteamVR keeps the boundaries drawn in it (chaperone_info.vrchap; it can hold
several). When the Frame recognises the map it restores the boundary that was
in use when the map was saved ("Relocalized: universeId ..." in the log).
Setting up somewhere else, or a failed match, starts a new map.

A saved room is a copy of the map + boundaries, with a name and a top-down
outline of the boundary in use (for the preview). Loading one swaps them in
while SteamVR is stopped (XRService writes its map on exit, so the swap has to
happen after it quits), then starts SteamVR so the Frame relocalises into the
saved map and restores that boundary. The room that was in use becomes the
backup ("Undo last load"); loading the backup swaps the two again, so an undo
can be undone. If SteamVR doesn't come back up, the previous room is swapped
back. Tested 2026-11-22: restored 2 s after SteamVR came back.

Folders are swapped whole (renamed, never emptied file by file), so a failure
or a power cut at any point leaves either the old room or the new one in place.

    rooms.py --apply <room_id>   (run detached by the app: stop SteamVR, swap, start)
    rooms.py --list
"""
import errno
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

CFG = os.path.expanduser("~/.config/openvr/config")
VRCHAP = os.path.join(CFG, "chaperone_info.vrchap")
MAP_DIR = os.path.join(CFG, "cv", "xrservice", "serializedmap")
ROOMS = os.path.expanduser("~/.local/share/frame-settings/rooms")
BACKUP = "_before_last_load"
LOG = os.path.join(ROOMS, "rooms.log")
SUGGESTED_NAMES = ["Bedroom", "Living room", "Office", "Kitchen", "Garage"]
DUMMY_UNIVERSES = {"424242", "525252"}  # the Frame's stand-in boundaries while it isn't tracking a room
UNIVERSE_LINES = ("Relocalized: universeId|Created and Switched to a chaperone universe|"
                  "Restored chaperone universe")
UNIVERSE_ID = re.compile(r"(?:universeId|universe)\s+(\d{6,})")
STEAMVR = "steamvr.service"
NEW = ".deckhand-new"  # a room being swapped in sits next to the live one under this suffix


def log(msg):
    try:
        os.makedirs(ROOMS, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


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


def active_universe_id(timeout=15):
    """The boundary in use: the newest universe the tracking service relocalised into, restored
    or created this boot (None if the log has none). Reads the journal newest first and stops at
    the first real id, so it stays quick after days of uptime; gives up after `timeout` s."""
    try:
        p = subprocess.Popen(["journalctl", "-b", "-r", "-o", "cat", "--no-pager", "-g", UNIVERSE_LINES],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="replace")
    except OSError:
        return None
    killer = threading.Timer(timeout, p.kill)
    killer.daemon = True
    killer.start()
    try:
        for line in p.stdout:
            # placeholders are used while tracking is lost or in standby: skip them
            ids = [i for i in UNIVERSE_ID.findall(line) if i not in DUMMY_UNIVERSES]
            if ids:
                return ids[-1]
        return None
    finally:
        killer.cancel()
        p.kill()  # found it: no need to read the rest of the journal
        p.stdout.close()
        p.wait()


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
    except (OSError, ValueError, AttributeError, TypeError, IndexError):
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
    rid = str(room_id).replace("/", "_")
    if not rid or rid.startswith("."):  # also "." and "..": never the folder itself or its parent
        raise ValueError(f"not a room: {room_id!r}")
    return os.path.join(ROOMS, rid)


def list_rooms():
    """Saved rooms, newest first: [{'id', 'name', 'saved', ...describe()}] (backup excluded)."""
    out = []
    try:
        names = os.listdir(ROOMS)
    except OSError:
        return out
    for rid in names:
        if rid == BACKUP or rid.startswith(".") or rid.endswith(".tmp"):  # dot names: work in progress
            continue
        meta = os.path.join(ROOMS, rid, "meta.json")
        try:
            with open(meta, encoding="utf-8") as f:
                m = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(m, dict):
            continue
        if not m.get("outline"):  # saved by an older version: fill in the preview data once
            m.update(describe(os.path.join(ROOMS, rid, "chaperone_info.vrchap"),
                              os.path.join(ROOMS, rid, "serializedmap")))
            try:
                _write_meta(rid, m)
            except OSError:
                pass
        m["id"] = rid
        out.append(m)
    return sorted(out, key=lambda m: -m["saved"] if isinstance(m.get("saved"), (int, float)) else 0)


def backup_exists():
    return os.path.isfile(os.path.join(room_dir(BACKUP), "meta.json"))


def _write_meta_at(folder, meta):
    path = os.path.join(folder, "meta.json")
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in meta.items() if k != "id"}, f, indent=2)
    os.replace(tmp, path)


def _write_meta(rid, meta):
    _write_meta_at(room_dir(rid), meta)


def _new_tmp():
    """A fresh, private work folder in ROOMS (hidden from the list by its dot name)."""
    os.makedirs(ROOMS, exist_ok=True)
    return tempfile.mkdtemp(prefix=".tmp-", dir=ROOMS)


def _aside(path):
    """An unused name next to `path`, to rename it to before deleting it."""
    return os.path.join(os.path.dirname(path), f".deckhand-old-{uuid.uuid4().hex[:12]}")


def _remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _move(src, dst):
    """Moves a folder: a rename (instant) on the same disk; copy, then delete, across disks."""
    try:
        os.rename(src, dst)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        shutil.copytree(src, dst)
        gone = _aside(src)
        os.rename(src, gone)
        shutil.rmtree(gone, ignore_errors=True)


def _tidy():
    """Removes work folders that a crash or power cut left behind."""
    try:
        names = os.listdir(ROOMS)
    except OSError:
        return
    for n in names:
        p = os.path.join(ROOMS, n)
        try:
            # only old ones: a fresh one may belong to a save or load that is running right now
            # (renaming a folder updates its ctime, so ctime dates a renamed-aside copy)
            st = os.stat(p)
            if ((n.startswith(".deckhand-old-") and time.time() - st.st_ctime > 3600)
                    or ((n.startswith(".tmp-") or n.endswith(".tmp")) and time.time() - st.st_mtime > 86400)):
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass


def _promote(tmp, dest):
    """Puts the finished folder `tmp` in place of `dest`: dest is renamed aside first and deleted
    last, so a failure never leaves a half-deleted room. Every save builds in its own folder, so
    two saves at once can't mix their files (the last one wins)."""
    for _ in range(5):
        old = None
        if os.path.lexists(dest):
            old = _aside(dest)
            try:
                os.rename(dest, old)
            except FileNotFoundError:
                old = None  # another save moved it meanwhile
        try:
            os.rename(tmp, dest)
        except OSError:
            if not os.path.lexists(dest):
                if old:
                    os.rename(old, dest)  # put the previous copy back
                raise
            if old:  # another save put its copy there meanwhile: go round again
                shutil.rmtree(old, ignore_errors=True)
            continue
        if old:
            shutil.rmtree(old, ignore_errors=True)
        return
    raise RuntimeError("the saved room kept changing; try again")


def _snapshot(*places):
    """(size, mtime) of every file in the given files and folders: notices a change mid-copy."""
    out = {}
    for place in places:
        files = [place] if os.path.isfile(place) else [
            os.path.join(root, f) for root, _dirs, names in os.walk(place) for f in names]
        for p in files:
            try:
                st = os.stat(p)
            except OSError:
                continue
            out[p] = (st.st_size, st.st_mtime_ns)
    return out


def _copy_live(folder):
    """Copies the live map + boundaries into `folder`, again if they change while copying."""
    for attempt in range(3):
        target = os.path.join(folder, "serializedmap")
        shutil.rmtree(target, ignore_errors=True)
        try:
            before = _snapshot(MAP_DIR, VRCHAP)
            shutil.copytree(MAP_DIR, target)
            shutil.copy2(VRCHAP, os.path.join(folder, "chaperone_info.vrchap"))
            if _snapshot(MAP_DIR, VRCHAP) == before:
                return
            log(f"map changed while copying (attempt {attempt + 1}); retrying")
        except OSError as e:  # a file replaced mid-copy (shutil.Error is an OSError too)
            log(f"copy failed (attempt {attempt + 1}): {e}")
    raise RuntimeError("the room map kept changing while saving; try again in a moment")


def _copy_room(dest, name, universe):
    """Saves the live room as folder `dest`: built in a private work folder, then swapped in
    whole (see _promote). Returns its meta."""
    tmp = _new_tmp()
    try:
        _copy_live(tmp)
        meta = {"name": name, "saved": time.time(),
                **describe(os.path.join(tmp, "chaperone_info.vrchap"), os.path.join(tmp, "serializedmap"),
                           universe)}
        _write_meta_at(tmp, meta)
        _promote(tmp, dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return meta


def find_by_name(name):
    for m in list_rooms():
        if str(m.get("name", "")).strip().lower() == name.strip().lower():
            return m["id"]
    return None


def save(name, room_id=None):
    """Saves the room in use as `name` (replacing a room with that name, or room_id)."""
    name = name.strip()[:40] or "Room"
    if not os.path.isfile(VRCHAP) or not os.path.isdir(MAP_DIR):
        raise RuntimeError("no room to save yet (no map or boundary on this headset)")
    rid = room_id or find_by_name(name) or (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "room") + \
        time.strftime("-%Y%m%d-%H%M%S")
    _tidy()
    meta = _copy_room(room_dir(rid), name, active_universe_id())
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
    d = room_dir(room_id)
    gone = _aside(d)
    try:
        os.rename(d, gone)  # out of the list at once, then deleted
    except OSError:
        gone = d
    shutil.rmtree(gone, ignore_errors=True)
    log(f"deleted {room_id}")


def start_load(room_id):
    """Runs the load as its own background job (it stops SteamVR, which closes the app).

    Always the full, graceful load: SteamVR is asked to stop, the room's files are swapped in,
    SteamVR starts again (~12 s). An earlier "quick" load that froze and killed the tracking
    service was removed: killing it while the cameras stream can leave the camera hardware in a
    bad state until the next reboot."""
    try:
        if not os.path.isfile(os.path.join(room_dir(room_id), "meta.json")):
            return False, "that room isn't saved"
    except ValueError:
        return False, "that room isn't saved"
    subprocess.run(["systemctl", "--user", "reset-failed", "frame-room-load"], capture_output=True)
    r = subprocess.run(["systemd-run", "--user", "--quiet", "--collect", "--unit=frame-room-load",
                        "--description=Deckhand: load a saved room",
                        sys.executable, os.path.abspath(__file__), "--apply", room_id],
                       capture_output=True, text=True)
    return r.returncode == 0, (r.stdout + r.stderr).strip() or "loading"


# ------------------------------------------------------------- loading ---
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


def _wait_gone(names, timeout=40):
    for _ in range(timeout * 2):
        if not any(_pid(n) for n in names):
            return True
        time.sleep(0.5)
    return False


def _wait_up(timeout=60, settle=5):
    """True once vrserver and the tracking service both run, the same processes for `settle` s
    (one that crashes straight away doesn't count)."""
    end = time.monotonic() + timeout
    seen, since = None, 0.0
    while time.monotonic() < end:
        pids = (_pid("vrserver"), _pid("XRService"))
        if all(pids):
            if pids != seen:
                seen, since = pids, time.monotonic()
            elif time.monotonic() - since >= settle:
                return True
        else:
            seen = None
        time.sleep(1)
    return False


def _steamvr(action):
    """systemctl --user <action> steamvr.service (graceful). True if systemd reports success."""
    try:
        r = subprocess.run(["systemctl", "--user", action, STEAMVR], capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as e:
        log(f"SteamVR {action} failed: {e}")
        return False
    if r.returncode != 0:
        log(f"SteamVR {action} failed: {(r.stderr or r.stdout).strip()[:200]}")
    return r.returncode == 0


def _stop_steamvr():
    _steamvr("stop")
    return _wait_gone(["vrserver", "XRService"])


def _restore(folder):
    """With SteamVR stopped: makes the room in `folder` the live one again (used up), discarding
    the live map."""
    old = None
    if os.path.lexists(MAP_DIR):
        old = _aside(MAP_DIR)
        os.rename(MAP_DIR, old)
    _move(os.path.join(folder, "serializedmap"), MAP_DIR)
    chap = os.path.join(folder, "chaperone_info.vrchap")
    if os.path.isfile(chap):
        shutil.copy2(chap, VRCHAP + NEW)
        os.replace(VRCHAP + NEW, VRCHAP)
    if old:
        shutil.rmtree(old, ignore_errors=True)
    shutil.rmtree(folder, ignore_errors=True)


def _swap_in(src):
    """With SteamVR stopped: puts a copy of room folder `src` in place of the live map and
    boundaries. Returns a work folder (in ROOMS) holding the room that was in use."""
    staged, chap = MAP_DIR + NEW, VRCHAP + NEW
    shutil.rmtree(staged, ignore_errors=True)
    shutil.copytree(os.path.join(src, "serializedmap"), staged)  # a copy: the saved room stays as it is
    shutil.copy2(os.path.join(src, "chaperone_info.vrchap"), chap)
    held = _new_tmp()
    if os.path.isfile(VRCHAP):
        shutil.copy2(VRCHAP, os.path.join(held, "chaperone_info.vrchap"))
    if os.path.isdir(MAP_DIR):
        _move(MAP_DIR, os.path.join(held, "serializedmap"))  # a rename: instant
    else:
        os.makedirs(os.path.join(held, "serializedmap"))  # no map yet: undo goes back to none
    try:
        os.rename(staged, MAP_DIR)
        os.replace(chap, VRCHAP)
    except BaseException:
        _restore(held)  # put the room in use back
        raise
    return held


def _keep_as_backup(held, universe):
    """The room that was just swapped out becomes "Undo last load" (replacing the last backup)."""
    _write_meta_at(held, {"name": "Before last load", "saved": time.time(),
                          **describe(os.path.join(held, "chaperone_info.vrchap"),
                                     os.path.join(held, "serializedmap"), universe)})
    _promote(held, room_dir(BACKUP))


def apply(room_id):
    """Loads a saved room, or the backup (Undo). Returns the exit status of the --apply job:
    0 = loaded and SteamVR is back up."""
    try:
        src = room_dir(room_id)
    except ValueError as e:
        log(f"load: {e}")
        return 2
    if not (os.path.isdir(os.path.join(src, "serializedmap"))
            and os.path.isfile(os.path.join(src, "chaperone_info.vrchap"))):
        log(f"load {room_id}: that room isn't saved (or is incomplete); nothing changed")
        return 1
    _tidy()
    universe_before = active_universe_id()
    log(f"load {room_id}: stopping SteamVR")
    if not _stop_steamvr():
        log("SteamVR did not stop in time; nothing swapped, starting it again")
        _steamvr("start")
        return 1
    try:
        held = _swap_in(src)
    except Exception as e:  # noqa: BLE001
        log(f"load {room_id} failed while swapping; the room in use is unchanged: {e}")
        shutil.rmtree(MAP_DIR + NEW, ignore_errors=True)
        _remove(VRCHAP + NEW)
        _steamvr("start")
        return 1
    try:
        _keep_as_backup(held, universe_before)
    except Exception as e:  # noqa: BLE001
        log(f"load {room_id}: couldn't keep the room in use as the backup, so it stays: {e}")
        try:
            _restore(held)
        except Exception as e2:  # noqa: BLE001
            log(f"couldn't put it back either ({e2}); it is in {held}")
        _steamvr("start")
        return 1
    log(f"load {room_id}: swapped in, starting SteamVR")
    if _steamvr("start") and _wait_up():
        log(f"load {room_id}: done, SteamVR is back")
        return 0
    # SteamVR didn't come back with this room: swap the previous one back in (a swap, so nothing
    # is lost: the room that failed becomes the backup, where an undo would have come from)
    log(f"load {room_id}: SteamVR didn't come back up; switching back to the previous room")
    if not _stop_steamvr():
        log("SteamVR did not stop; leaving it as it is (Undo last load has the previous room)")
        return 1
    try:
        _keep_as_backup(_swap_in(room_dir(BACKUP)), None)
        log("previous room is back")
    except Exception as e:  # noqa: BLE001
        log(f"couldn't switch back: {e}")
    _steamvr("start")
    return 1


if __name__ == "__main__":
    if sys.argv[1:2] == ["--apply"] and len(sys.argv) == 3:
        sys.exit(apply(sys.argv[2]))
    elif sys.argv[1:] == ["--list"]:
        print(json.dumps({"current": current(), "saved": list_rooms()}, indent=2)[:4000])
    else:
        print(__doc__)
