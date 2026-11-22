#!/usr/bin/env python3
"""Power features for Deckhand: shut down now, sleep timer, auto shut-off.

Why: taking the Frame off or tapping the power button only puts SteamVR in
*standby* (screens off). The Frame itself stays on (CPU, Wi-Fi, fan, SteamVR)
and drains the battery. This shuts it down for real.

- As a module, the app uses: load_config / save_config / read_status /
  shutdown / ensure_service / service_active (and charging()).
- As a service (`power.py --service`, run by the systemd user unit
  frame-autooff.service) it watches SteamVR's standby events in the journal and
  shuts the Frame down when:
    * the headset has been in standby for `idle_off_minutes` (0 = off). With
      `skip_while_charging` there is no countdown while plugged in, and it starts
      again from when the Frame was unplugged. It waits while a kitchen timer
      runs or a reminder is due within that time.
    * the sleep timer (`timer_deadline`, epoch seconds) runs out (with a SteamVR
      notification one minute before). The timer belongs to the boot it was set
      in (`timer_boot`): a power-off ends it.

Shutdown goes through Steam's own SteamClient.System.ShutdownPC() (no password,
Steam closes cleanly), falling back to the root helper's "poweroff".
"""
import json
import math
import os
import subprocess
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
import paths  # noqa: E402
CFG_DIR = os.path.expanduser("~/.config/frame-settings")
CFG = os.environ.get("FRAME_POWER_CONFIG") or os.path.join(CFG_DIR, "power.json")  # override for tests
STATUS = os.path.expanduser("~/.cache/frame-settings/power-status.json")
LOG = os.path.join(paths.LOGS, "power.log")
HELPER = "/etc/frame-settings/fanmode"
UNIT_NAME = "frame-autooff.service"
UNIT_PATH = os.path.expanduser(f"~/.config/systemd/user/{UNIT_NAME}")
DEFAULTS = {"idle_off_minutes": 15, "skip_while_charging": True, "timer_deadline": None, "timer_boot": None}
STANDBY_FILTER = ["SYSLOG_IDENTIFIER=vrserver", "-g", "(entering|leaving) standby"]
LOOP_SECONDS = 20
STATUS_REFRESH = 300  # rewrite the unchanged status file at most every 5 min (spares flash writes)
POWER_SUPPLY = "/sys/class/power_supply"
BOOT_ID_FILE = "/proc/sys/kernel/random/boot_id"
REMINDER_GRACE = 90  # a reminder that just came due keeps the Frame on this long (it rings for 30 s)
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


# ------------------------------------------------------------ helpers ---
def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        if os.path.exists(LOG) and os.path.getsize(LOG) > 200_000:
            os.replace(LOG, LOG + ".old")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except (OSError, ValueError):
        return ""


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"  # per process: the app and the service may write at once
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


_boot = None


def boot_id():
    """This boot's id (new at every start of the Frame), or None where there is none."""
    global _boot
    if _boot is None:
        _boot = _read(BOOT_ID_FILE)
    return _boot or None


def _boot_time():
    """When this boot started (epoch seconds), or None."""
    try:
        with open("/proc/stat", encoding="utf-8") as f:
            return int(next(line for line in f if line.startswith("btime")).split()[1])
    except (OSError, ValueError, IndexError, StopIteration):
        return None


# ------------------------------------------------------------- config ---
# A hand-edited or damaged file must not crash the service or the app: every value is read as
# the type it should be, falling back to the default.
def _as_minutes(v):
    if v is None:
        return 0  # null: off (that's how the app shows it)
    try:
        f = float(v) if not isinstance(v, bool) else math.nan
    except (TypeError, ValueError):
        f = math.nan
    if not math.isfinite(f):
        return DEFAULTS["idle_off_minutes"]
    return max(0, min(int(f), 24 * 60))


def _as_bool(v, default):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off", ""):
            return False
    return default


def _as_time(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def _timer_from_this_boot(stamp):
    now = boot_id()
    if not now:
        return True  # no boot id here (not Linux): can't tell, keep it
    if stamp:
        return stamp == now
    # set by an older version (no stamp): only if the file was written since this boot started
    started = _boot_time()
    try:
        return started is not None and os.path.getmtime(CFG) >= started
    except OSError:
        return False


def load_config():
    """Settings, each of its expected type. A sleep timer set before the Frame last started is
    dropped: it was meant for that session, and would shut the Frame down right after booting."""
    raw = {}
    try:
        with open(CFG, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        pass
    cfg = dict(DEFAULTS)
    if isinstance(raw, dict):
        cfg.update(raw)
    cfg["idle_off_minutes"] = _as_minutes(cfg["idle_off_minutes"])
    cfg["skip_while_charging"] = _as_bool(cfg["skip_while_charging"], DEFAULTS["skip_while_charging"])
    cfg["timer_deadline"] = _as_time(cfg["timer_deadline"])
    cfg["timer_boot"] = cfg["timer_boot"] if isinstance(cfg["timer_boot"], str) else None
    if cfg["timer_deadline"] and not _timer_from_this_boot(cfg["timer_boot"]):
        cfg["timer_deadline"] = cfg["timer_boot"] = None
    return cfg


def save_config(cfg):
    out = {k: cfg.get(k, v) for k, v in DEFAULTS.items()}
    out["idle_off_minutes"] = _as_minutes(out["idle_off_minutes"])
    out["skip_while_charging"] = _as_bool(out["skip_while_charging"], DEFAULTS["skip_while_charging"])
    out["timer_deadline"] = _as_time(out["timer_deadline"])
    out["timer_boot"] = boot_id() if out["timer_deadline"] else None  # the sleep timer is for this boot only
    _write_json(CFG, out)


def read_status():
    try:
        with open(STATUS, encoding="utf-8") as f:
            st = json.load(f)
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


# -------------------------------------------------------------- state ---
def charging():
    """True while the Frame is plugged in: its battery says Charging, Full or Not charging, or a
    mains/USB supply is online. Batteries of other devices (controllers: scope Device) don't count.
    Each supply is read on its own: one without a status file doesn't end the scan."""
    try:
        names = os.listdir(POWER_SUPPLY)
    except OSError:
        return False
    for n in names:
        d = os.path.join(POWER_SUPPLY, n)
        if _read(os.path.join(d, "scope")) == "Device":
            continue
        kind = _read(os.path.join(d, "type"))
        if kind == "Battery":
            if _read(os.path.join(d, "status")) in ("Charging", "Full", "Not charging"):
                return True
        elif (kind == "Mains" or kind.startswith("USB")) and _read(os.path.join(d, "online")) == "1":
            return True
    return False


def _proc_stat(pid):
    """(name, start time in clock ticks after boot) of a process, or None."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
        head, tail = data.rsplit(b")", 1)
        return head.split(b"(", 1)[1].decode(errors="replace"), int(tail.split()[19])
    except (OSError, ValueError, IndexError):
        return None


def vrserver_proc(known_pid=None):
    """(pid, start ticks) of SteamVR's server, or (None, None). A pid found earlier is checked
    first: one small file read instead of a scan of every process."""
    if known_pid:
        st = _proc_stat(known_pid)
        if st and st[0] == "vrserver":
            return known_pid, st[1]
    best = (None, None)
    try:
        pids = [d for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return best
    for d in pids:
        st = _proc_stat(d)
        if st and st[0] == "vrserver" and (best[1] is None or st[1] > best[1]):
            best = (int(d), st[1])
    return best


def _ticks_to_epoch(ticks):
    started = _boot_time()
    return None if started is None or ticks is None else started + ticks / CLK_TCK


def vrserver_pid_and_start():
    """(pid, start time in epoch seconds) of SteamVR's server, or (None, None)."""
    pid, ticks = vrserver_proc()
    return (pid, _ticks_to_epoch(ticks)) if pid else (None, None)


def vrcmd():
    for p in ("/opt/steamvr/bin/linuxarm64/vrcmd", "/opt/steamvr/bin/linux64/vrcmd"):
        if os.access(p, os.X_OK):
            return p
    return None


def notify(text):
    """SteamVR notification; only while SteamVR runs (vrcmd would start it otherwise)."""
    exe = vrcmd()
    if not exe or not vrserver_proc()[0]:
        return
    try:  # clean environment: vrcmd must not connect to SteamVR as Deckhand (Steam's app id)
        subprocess.run([exe, "--notify", text], capture_output=True, timeout=15, env=paths.clean_env())
    except (OSError, subprocess.SubprocessError) as e:
        log(f"notification failed: {e}")


# ----------------------------------------------------------- shutdown ---
def _clear_sleep_timer():
    try:
        cfg = load_config()
        if cfg["timer_deadline"]:
            cfg["timer_deadline"] = None
            save_config(cfg)
    except OSError as e:
        log(f"couldn't clear the sleep timer: {e}")


def shutdown(reason):
    """Shuts the Frame down. Returns (False) only if every method failed."""
    log(f"shutting down: {reason}")
    _clear_sleep_timer()  # whatever the reason, a power-off ends the sleep timer
    try:
        import steam_shortcut  # Steam DevTools helper (same folder)
        steam_shortcut.evaluate("SteamClient.System.ShutdownPC(), 'ok'")
        log("asked Steam to shut down")
        time.sleep(60)
        log("still running 60 s after asking Steam; trying the root helper")
    except BaseException as e:  # noqa: BLE001 (SystemExit from evaluate included)
        log(f"Steam shutdown unavailable: {e}")
    for cmd in (["sudo", "-n", HELPER, "poweroff"], ["systemctl", "poweroff"]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            log(f"{' '.join(cmd)} failed: {e}")
            continue
        if r.returncode == 0:
            log(f"{' '.join(cmd)}: ok")
            time.sleep(60)
        else:
            log(f"{' '.join(cmd)} failed: {(r.stdout + r.stderr).strip()[:200]}")
    return False


def _dry_run_shutdown(reason):
    log(f"DRY RUN: would shut down: {reason}")
    print(f"DRY RUN: would shut down: {reason}", flush=True)
    time.sleep(LOOP_SECONDS)
    return False


# ------------------------------------------------------------ service ---
UNIT = f"""[Unit]
Description=Deckhand: auto shut-off and sleep timer
After=steamvr.service

[Service]
ExecStart=/usr/bin/python3 {os.path.join(HERE, "power.py")} --service
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
"""


def _systemctl(*args):
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(args, 1, "", str(e))


def service_active():
    return _systemctl("is-active", "--quiet", UNIT_NAME).returncode == 0


def ensure_service():
    """Installs/updates and starts the user service. Returns (ok, message)."""
    current = None
    try:
        with open(UNIT_PATH, encoding="utf-8") as f:
            current = f.read()
    except OSError:
        pass
    if current != UNIT:
        os.makedirs(os.path.dirname(UNIT_PATH), exist_ok=True)
        tmp = os.path.join(os.path.dirname(UNIT_PATH), f".{UNIT_NAME}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(UNIT)
        os.replace(tmp, UNIT_PATH)
        _systemctl("daemon-reload")
        _systemctl("enable", UNIT_NAME)
        _systemctl("restart", UNIT_NAME)
    elif not service_active():
        _systemctl("reset-failed", UNIT_NAME)
        _systemctl("enable", "--now", UNIT_NAME)
    time.sleep(1)
    return (True, "running") if service_active() else (False, "the auto shut-off service did not start")


class StandbyWatcher:
    """Tracks whether SteamVR is in standby, from its journal messages."""

    def __init__(self, follow=True):
        self.lock = threading.Lock()
        self.standby_since = None
        self.cursor = None  # the last journal entry read: a restarted reader carries on after it
        self.pid, self.ticks = vrserver_proc()
        self.started = _ticks_to_epoch(self.ticks)
        self.synced = time.time()
        self._initial_state()
        if follow:
            threading.Thread(target=self._follow, daemon=True).start()

    def _apply(self, entry):
        msg = entry.get("MESSAGE")
        try:
            ts = int(entry.get("__REALTIME_TIMESTAMP", "0")) / 1e6
        except (TypeError, ValueError):
            return
        with self.lock:
            if isinstance(entry.get("__CURSOR"), str):
                self.cursor = entry["__CURSOR"]
            if not isinstance(msg, str) or (self.started and ts < self.started):
                return  # unreadable, or from an earlier SteamVR run
            if "entering standby" in msg:
                self.standby_since = self.standby_since or ts
            elif "leaving standby" in msg:
                self.standby_since = None

    def _read(self, lines):
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                self._apply(entry)

    def _initial_state(self):
        """The last standby event of this boot (also where the journal reader starts)."""
        self.synced = time.time()
        try:
            r = subprocess.run(["journalctl", "-b", "-o", "json", "-n", "1", "--no-pager", *STANDBY_FILTER],
                               capture_output=True, text=True, errors="replace", timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            log(f"couldn't read the journal: {e}")
            return
        self._read(r.stdout.splitlines())

    def _follow(self):
        delay = 0
        while True:
            with self.lock:
                cursor = self.cursor
            # Start right after the last entry read (or the first look at the journal), so nothing
            # logged while no reader ran is missed.
            where = ["--after-cursor", cursor] if cursor else ["--since", f"@{int(self.synced) - 1}"]
            began = time.monotonic()
            try:
                p = subprocess.Popen(["journalctl", "-f", "--no-tail", "-o", "json", "--no-pager", *where,
                                      *STANDBY_FILTER], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, errors="replace")
            except OSError as e:
                result = str(e)
            else:
                try:
                    self._read(p.stdout)
                except Exception as e:  # noqa: BLE001 (keep the reader alive whatever happens)
                    log(f"journal reader error: {e}")
                finally:
                    p.kill()  # no-op if it has exited already
                    p.stdout.close()
                    result = f"exit code {p.wait()}"
            # Back off while it keeps failing (5 s, 10 s, ... up to 5 min); after a long run, 5 s again.
            delay = 5 if not delay or time.monotonic() - began > 300 else min(delay * 2, 300)
            log(f"journal reader stopped ({result}); starting it again in {delay} s")
            time.sleep(delay)

    def check_restart(self):
        """Follows SteamVR restarts and returns since when it's been in standby (None: in use)."""
        pid, ticks = vrserver_proc(self.pid)
        with self.lock:
            if (pid, ticks) != (self.pid, self.ticks):
                self.pid, self.ticks = pid, ticks
                self.started = _ticks_to_epoch(ticks) if pid else None
                # A new server starts out of standby, but keep an "entering standby" it has already
                # logged (the reader can see it before this check notices the new server).
                if not (pid and self.standby_since and self.started and self.standby_since >= self.started):
                    self.standby_since = None
            return self.standby_since if pid else None


def _keep_on_reason(window):
    """Why the idle shut-off should wait, or None: a kitchen timer is running, or a reminder is due
    within `window` seconds (or has just come due). Only file reads; no process is started."""
    try:
        import kitchen_timer
        if kitchen_timer.status():
            return "a kitchen timer is running"
    except Exception as e:  # noqa: BLE001 (a broken check must not block the shut-off)
        log(f"couldn't check the kitchen timer: {e}")
    try:
        import reminders
        now = time.time()
        if any(now - REMINDER_GRACE <= m["when"] <= now + window for m in reminders.upcoming()):
            return "a reminder is due"
    except Exception as e:  # noqa: BLE001
        log(f"couldn't check the reminders: {e}")
    return None


def _service_round(watcher, st, off):
    cfg = load_config()
    now = time.time()
    standby_since = watcher.check_restart()
    chg = charging()
    if chg:
        st["plugged"] = now
    minutes = cfg["idle_off_minutes"]
    skip = cfg["skip_while_charging"]
    # The countdown runs in standby only; with skip_while_charging not while plugged in, and it
    # starts again from when the Frame was unplugged.
    idle_from = None
    if minutes and standby_since and not (chg and skip):
        idle_from = max(standby_since, st["plugged"] or 0) if skip else standby_since
    due = idle_from is not None and now - idle_from >= minutes * 60
    held = _keep_on_reason(minutes * 60) if due else None
    if held and held != st["held"]:
        log(f"auto shut-off waits: {held}")
    st["held"] = held

    status = {"standby_since": standby_since, "charging": chg, "steamvr": watcher.pid is not None,
              "off_at": idle_from + minutes * 60 if idle_from is not None and not held else None,
              "held": held}
    if status != st["status"] or now - st["written"] > STATUS_REFRESH:
        _write_json(STATUS, {"updated": now, **status})
        st["status"], st["written"] = status, now

    deadline = cfg["timer_deadline"]
    if deadline:
        if now >= deadline:
            cfg["timer_deadline"] = None
            save_config(cfg)
            off("sleep timer ran out")
            return
        if deadline - now <= 75 and st["warned"] != deadline:
            st["warned"] = deadline
            notify("Frame shuts down in 1 minute (sleep timer). Cancel it in Deckhand.")
    if due and not held:
        off(f"in standby for {int((now - standby_since) / 60)} min")


def run_service(dry_run=False):
    off = _dry_run_shutdown if dry_run else shutdown
    log("auto shut-off service started" + (" (dry run)" if dry_run else ""))
    try:  # a sleep timer from before the last power-off: drop it from the file too
        with open(CFG, encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict) and raw.get("timer_deadline") and not load_config()["timer_deadline"]:
            log("ignoring a sleep timer set before the Frame last started")
            save_config(load_config())
    except (OSError, ValueError):
        pass
    watcher = StandbyWatcher()
    st = {"warned": None, "plugged": None, "held": None, "status": None, "written": 0.0}
    last_error = None
    while True:
        try:
            _service_round(watcher, st, off)
            last_error = None
        except Exception:  # noqa: BLE001 (one bad round must not stop the service)
            err = traceback.format_exc().rstrip()
            if err != last_error:  # the same error every 20 s is logged once
                log("error in the service loop:\n" + err)
            last_error = err
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--service"]:
        run_service(dry_run="--dry-run" in args)
    elif args == ["--state"]:
        w = StandbyWatcher(follow=False)
        since = w.check_restart()
        print(json.dumps({"vrserver_pid": w.pid, "vrserver_started": w.started, "standby_since": since,
                          "standby_minutes": round((time.time() - since) / 60, 1) if since else None,
                          "charging": charging(), "config": load_config()}))
    else:
        print(__doc__)
