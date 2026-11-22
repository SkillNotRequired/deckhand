#!/usr/bin/env python3
"""Power features for Deckhand: shut down now, sleep timer, auto shut-off.

Why: taking the Frame off or tapping the power button only puts SteamVR in
*standby* (screens off). The Frame itself stays on (CPU, Wi-Fi, fan, SteamVR)
and drains the battery. This shuts it down for real.

- As a module, the app uses: load_config / save_config / read_status /
  shutdown / ensure_service / service_active.
- As a service (`power.py --service`, run by the systemd user unit
  frame-autooff.service) it watches SteamVR's standby events in the journal and
  shuts the Frame down when:
    * the headset has been in standby for `idle_off_minutes` (0 = off),
      unless it is charging and `skip_while_charging` is set, or
    * the sleep timer (`timer_deadline`, epoch seconds) runs out
      (with a SteamVR notification one minute before).

Shutdown goes through Steam's own SteamClient.System.ShutdownPC() (no password,
Steam closes cleanly), falling back to the root helper's "poweroff".
"""
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_DIR = os.path.expanduser("~/.config/frame-settings")
CFG = os.environ.get("FRAME_POWER_CONFIG") or os.path.join(CFG_DIR, "power.json")  # override for tests
STATUS = os.path.expanduser("~/.cache/frame-settings/power-status.json")
LOG = os.path.join(HERE, "power.log")
HELPER = "/etc/frame-settings/fanmode"
UNIT_NAME = "frame-autooff.service"
UNIT_PATH = os.path.expanduser(f"~/.config/systemd/user/{UNIT_NAME}")
DEFAULTS = {"idle_off_minutes": 15, "skip_while_charging": True, "timer_deadline": None}
STANDBY_FILTER = ["SYSLOG_IDENTIFIER=vrserver", "-g", "(entering|leaving) standby"]
LOOP_SECONDS = 20
STATUS_REFRESH = 300  # rewrite the unchanged status file at most every 5 min (spares flash writes)


# ------------------------------------------------------------ helpers ---
def log(msg):
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 200_000:
            os.replace(LOG, LOG + ".old")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CFG, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    _write_json(CFG, {k: cfg.get(k, v) for k, v in DEFAULTS.items()})


def read_status():
    try:
        with open(STATUS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def charging():
    base = "/sys/class/power_supply"
    try:
        for n in os.listdir(base):
            with open(f"{base}/{n}/status", encoding="utf-8") as f:
                if f.read().strip() == "Charging":
                    return True
    except OSError:
        pass
    return False


def vrserver_pid_and_start():
    """(pid, start time in epoch seconds) of SteamVR's server, or (None, None)."""
    try:
        pid = int(subprocess.run(["pgrep", "-x", "vrserver"], capture_output=True, text=True).stdout.split()[0])
        with open(f"/proc/{pid}/stat") as f:
            ticks = int(f.read().rsplit(")", 1)[1].split()[19])
        with open("/proc/stat") as f:
            btime = int(next(l for l in f if l.startswith("btime")).split()[1])
        return pid, btime + ticks / os.sysconf("SC_CLK_TCK")
    except (IndexError, ValueError, OSError, StopIteration):
        return None, None


def vrcmd():
    for p in ("/opt/steamvr/bin/linuxarm64/vrcmd", "/opt/steamvr/bin/linux64/vrcmd"):
        if os.access(p, os.X_OK):
            return p
    return None


def notify(text):
    """SteamVR notification; only while SteamVR runs (vrcmd would start it otherwise)."""
    if vrserver_pid_and_start()[0] and vrcmd():
        subprocess.run([vrcmd(), "--notify", text], capture_output=True, timeout=15)


# ----------------------------------------------------------- shutdown ---
def shutdown(reason):
    """Shuts the Frame down. Returns only if every method failed."""
    log(f"shutting down: {reason}")
    try:
        sys.path.insert(0, HERE)
        import steam_shortcut  # Steam DevTools helper (same folder)
        steam_shortcut.evaluate("SteamClient.System.ShutdownPC(), 'ok'")
        log("asked Steam to shut down")
        time.sleep(60)
        log("still running 60 s after asking Steam; trying the root helper")
    except BaseException as e:  # noqa: BLE001 (SystemExit from evaluate included)
        log(f"Steam shutdown unavailable: {e}")
    for cmd in (["sudo", "-n", HELPER, "poweroff"], ["systemctl", "poweroff"]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            log(f"{' '.join(cmd)}: ok")
            time.sleep(60)
        else:
            log(f"{' '.join(cmd)} failed: {(r.stdout + r.stderr).strip()[:200]}")
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


def service_active():
    return subprocess.run(["systemctl", "--user", "is-active", "--quiet", UNIT_NAME]).returncode == 0


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
        with open(UNIT_PATH, "w", encoding="utf-8") as f:
            f.write(UNIT)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        subprocess.run(["systemctl", "--user", "enable", UNIT_NAME], capture_output=True)
        subprocess.run(["systemctl", "--user", "restart", UNIT_NAME], capture_output=True)
    elif not service_active():
        subprocess.run(["systemctl", "--user", "enable", "--now", UNIT_NAME], capture_output=True)
    time.sleep(1)
    return (True, "running") if service_active() else (False, "the auto shut-off service did not start")


class StandbyWatcher:
    """Tracks whether SteamVR is in standby, from its journal messages."""

    def __init__(self):
        self.lock = threading.Lock()
        self.standby_since = None
        self.pid, self.started = vrserver_pid_and_start()
        self._initial_state()
        threading.Thread(target=self._follow, daemon=True).start()

    def _apply(self, entry):
        msg = entry.get("MESSAGE", "")
        ts = int(entry.get("__REALTIME_TIMESTAMP", "0")) / 1e6
        with self.lock:
            if self.started and ts < self.started:
                return  # from an earlier SteamVR run
            if "entering standby" in msg:
                self.standby_since = self.standby_since or ts
            elif "leaving standby" in msg:
                self.standby_since = None

    def _initial_state(self):
        r = subprocess.run(["journalctl", "-b", "-o", "json", "-n", "1", "--no-pager", *STANDBY_FILTER],
                           capture_output=True, text=True)
        for line in r.stdout.splitlines():
            try:
                self._apply(json.loads(line))
            except ValueError:
                pass

    def _follow(self):
        while True:
            p = subprocess.Popen(["journalctl", "-f", "-n", "0", "-o", "json", "--no-pager", *STANDBY_FILTER],
                                 stdout=subprocess.PIPE, text=True)
            for line in p.stdout:
                try:
                    self._apply(json.loads(line))
                except ValueError:
                    pass
            time.sleep(5)

    def check_restart(self):
        """SteamVR restarted: it starts out of standby."""
        pid, started = vrserver_pid_and_start()
        with self.lock:
            if pid != self.pid:
                self.pid, self.started = pid, started
                self.standby_since = None
            return self.standby_since if pid else None


def run_service(dry_run=False):
    global shutdown
    if dry_run:
        def shutdown(reason):  # noqa: F811 (test mode: log instead of powering off)
            log(f"DRY RUN: would shut down: {reason}")
            print(f"DRY RUN: would shut down: {reason}", flush=True)
            time.sleep(LOOP_SECONDS)
    log("auto shut-off service started" + (" (dry run)" if dry_run else ""))
    watcher = StandbyWatcher()
    warned_deadline = None
    last_status, last_written = None, 0.0
    while True:
        cfg = load_config()
        now = time.time()
        standby_since = watcher.check_restart()
        chg = charging()
        status = {"standby_since": standby_since, "charging": chg, "steamvr": watcher.pid is not None}
        if status != last_status or now - last_written > STATUS_REFRESH:
            _write_json(STATUS, {"updated": now, **status})
            last_status, last_written = status, now

        deadline = cfg.get("timer_deadline")
        if deadline:
            if now >= deadline:
                cfg["timer_deadline"] = None
                save_config(cfg)
                shutdown("sleep timer ran out")
            elif deadline - now <= 75 and warned_deadline != deadline:
                warned_deadline = deadline
                notify("Frame shuts down in 1 minute (sleep timer). Cancel it in Deckhand.")

        minutes = cfg.get("idle_off_minutes") or 0
        if minutes and standby_since and now - standby_since >= minutes * 60:
            if chg and cfg.get("skip_while_charging", True):
                pass
            else:
                shutdown(f"in standby for {int((now - standby_since) / 60)} min")
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--service"]:
        run_service(dry_run="--dry-run" in args)
    elif args == ["--state"]:
        w = StandbyWatcher()
        since = w.check_restart()
        print(json.dumps({"vrserver_pid": w.pid, "vrserver_started": w.started, "standby_since": since,
                          "standby_minutes": round((time.time() - since) / 60, 1) if since else None,
                          "charging": charging(), "config": load_config()}))
    else:
        print(__doc__)
