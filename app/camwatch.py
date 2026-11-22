#!/usr/bin/env python3
"""Camera watch: writes what happens to the cameras and SteamVR into Deckhand's debug log.

Runs as the user service frame-camwatch.service while the debug log is on (the default), also
while Deckhand itself is closed. It follows the system journal and logs:
  camera    camera processor (ICP) faults, memory-unit (SMMU) faults, colour camera (arcimx616)
            drop-outs and errors, camera driver warnings, passthrough / streaming changes reported
            by the tracking service (XRService)
  headset   put on (out of standby) / taken off (standby)
  steamvr   SteamVR stopping, starting and failing, processes left over from its last run, its
            processes (server, compositor, tracking service) appearing and ending
  crash     any program that crashed (core dump), with what was running at that moment
  steam     apps Steam starts and closes (e.g. a camera app)
  helper    messages from Deckhand's own helpers (popups, mini-map, ceiling, position tracker)
  deckhand  Deckhand's processes appearing and ending, room loads, the auto shut-off service
  status    every minute while the headset is on (every 10 min otherwise): temperatures, fan,
            battery, the camera link error rate, dropped frames, the counts for this boot
At each camera fault or crash it adds what Deckhand was running at that moment, how long SteamVR's
processes had been up, and the temperatures.

It only reads (journal, /proc, the fan log, battery files): no SteamVR or camera calls, so it can't
disturb what it watches. Light: nice 19, idle I/O, a /proc scan every 5 s.

    camwatch.py --service        the service
    camwatch.py --status         this boot's figures (JSON)
    camwatch.py --replay FILE    feed `journalctl -o json` output through the filters (testing)
"""
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import debuglog  # noqa: E402

UNIT = "frame-camwatch.service"
UNIT_PATH = os.path.expanduser(f"~/.config/systemd/user/{UNIT}")
UNIT_TEXT = f"""[Unit]
Description=Deckhand: camera and app debug log

[Service]
ExecStart=/usr/bin/python3 {os.path.join(HERE, "camwatch.py")} --service
Restart=always
RestartSec=10
Nice=19
IOSchedulingClass=idle

[Install]
WantedBy=default.target
"""
STATE = os.path.expanduser("~/.cache/frame-settings/camwatch-state.json")
FAN_LOG = "/var/log/deckard-fan-control.log"
QUIET_DROPIN = "/etc/systemd/system/deckard-fan-control.service.d/quiet.conf"
MATCHES = ["_TRANSPORT=kernel", "SYSLOG_IDENTIFIER=XRService", "SYSLOG_IDENTIFIER=systemd-coredump",
           "USER_UNIT=steamvr.service", "SYSLOG_IDENTIFIER=console_log", "SYSLOG_IDENTIFIER=timer_overlay",
           "SYSLOG_IDENTIFIER=ceiling_overlay", "SYSLOG_IDENTIFIER=space_tool", "SYSLOG_IDENTIFIER=v4l2cam",
           "_SYSTEMD_USER_UNIT=frame-room-load.service", "_SYSTEMD_USER_UNIT=frame-autooff.service",
           "_SYSTEMD_USER_UNIT=arcturus-camera-control.service"]

# Kernel lines that concern the cameras (word starts, so "display" doesn't count as "isp").
KERNEL_CAMERA = re.compile(r"\b(cam\w*|isp|csi\w*|icp|ov\d{4}\w*|imx\d*\w*|v4l2\w*|arcimx\w*)\b", re.I)
KERNEL_SYSTEM = re.compile(r"thermal|throttl|critical temp|over.?temp|out of memory|oom-kill|\bBUG:|\bOops\b|"
                           r"watchdog|hung task|lockup", re.I)
# Tracking service lines worth a line in the log: (text in the line, event name).
XR_EVENTS = [("Passthrough cameras resumed", "passthrough"), ("Passthrough cameras paused", "passthrough"),
             ("passthrough camera polling thread", "passthrough"), ("Room View", "passthrough"),
             ("Tracking cameras streaming paused", "tracking-cam"), ("Tracking cameras streaming resumed", "tracking-cam"),
             ("Started streaming", "streaming"), ("Stopped streaming", "streaming"),
             ("Color passthrough cameras connected", "colour-cam"), ("Failed to read OTP data", "colour-cam"),
             ("Setting camera streaming mode", "stream-mode")]
XR_TROUBLE = re.compile(r"\b(error|fail\w*|fault|timeout|timed out|crash\w*|abort\w*|lost)\b", re.I)
XR_NOISE = re.compile(r"Just for logging|Max number of iteration|No group write applied|Failed to find interface|"
                      r"Pose Translation norm|Opened LED fault file", re.I)
FRIENDLY = {"XRServiceLoopTh": "tracking service (XRService)", "vrcompositor": "SteamVR compositor",
            "vrserver": "SteamVR server", "AVCameraMainThr": "Arcturus Vision Camera", "vrdashboard": "SteamVR dashboard",
            "timer_overlay": "Deckhand popup helper", "ceiling_overlay": "Deckhand ceiling indicator",
            "space_tool": "Deckhand position tracker"}


# ------------------------------------------------------------- readings ---
def proc_age(pid):
    """Seconds since process `pid` started, or None."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            start = int(f.read().rsplit(")", 1)[1].split()[19])
        with open("/proc/uptime", encoding="utf-8") as f:
            up = float(f.read().split()[0])
        return max(0.0, up - start / os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError, IndexError):
        return None


def describe(pid, comm):
    """A plain name for SteamVR's and Deckhand's processes; None for anything else."""
    if comm == "vrserver":
        return "SteamVR server"
    if comm == "vrcompositor":
        return "SteamVR compositor"
    try:
        if comm.startswith("XRService") or os.readlink(f"/proc/{pid}/exe").endswith("/XRService"):
            return "tracking service"
    except OSError:
        pass
    if comm not in ("timer_overlay", "space_tool", "ceiling_overlay", "python3", "python"):
        return None
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            argv = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return None
    if comm == "timer_overlay":
        mode = argv[1] if len(argv) > 1 else ""
        if mode == "--live":
            return "Deckhand readout in view"
        if mode == "--minimap":
            return "Deckhand mini-map in view"
        return "Deckhand reminder popup" if "remind" in " ".join(argv[2:3]) else "Deckhand timer popup"
    if comm == "space_tool":
        return "Deckhand position tracker" if "track" in argv else "Deckhand boundary reader"
    if comm == "ceiling_overlay":
        return "Deckhand ceiling indicator"
    script = " ".join(argv[1:3])
    for name, what in (("deckhand.py", "Deckhand app"), ("power.py", "Deckhand auto shut-off"),
                       ("rooms.py", "Deckhand room load"), ("updater.py", "Deckhand updater"),
                       ("reminders.py", "Deckhand reminder")):
        if name in script:
            return what
    return None


def scan():
    """{pid: name} of SteamVR's and Deckhand's processes running now (this watch excluded)."""
    found = {}
    me = os.getpid()
    try:
        pids = [int(d) for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return found
    for pid in pids:
        if pid == me:
            continue
        try:
            with open(f"/proc/{pid}/comm", encoding="utf-8") as f:
                comm = f.read().strip()
        except OSError:
            continue
        name = describe(pid, comm)
        if name:
            found[pid] = name
    return found


def fan_row():
    """The newest line of Valve's fan log as {column: value} (temperatures, fan speed), or {}."""
    try:
        with open(FAN_LOG, "rb") as f:
            header = f.readline().decode("utf-8", "replace").strip().split(",")
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 4096))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return {}
    row = next((ln for ln in reversed(lines) if ln[:1].isdigit() and ln.count(",") == len(header) - 1), None)
    return dict(zip(header, row.split(","))) if row else {}


def temps_text(row=None):
    row = fan_row() if row is None else row
    if not row:
        return ""

    def hottest(prefix):
        vals = []
        for k, v in row.items():
            if re.fullmatch(prefix + r"\d+_TEMP", k):
                try:
                    vals.append(float(v))
                except ValueError:
                    pass
        return max(vals) if vals else None
    parts = []
    for name, val in (("CPU", hottest("C")), ("GPU", hottest("G")), ("heatsink", row.get("HEATSINK_TEMP"))):
        try:
            parts.append(f"{name} {float(val):.0f} °C")
        except (TypeError, ValueError):
            pass
    try:
        parts.append(f"fan {int(float(row.get('FAN_REAL', ''))):,} rpm "
                     f"({'Deckhand setting' if os.path.exists(QUIET_DROPIN) else 'stock'})")
    except ValueError:
        pass
    return ", ".join(parts)


def battery():
    try:
        for n in sorted(os.listdir("/sys/class/power_supply")):
            p = f"/sys/class/power_supply/{n}"
            if _read(f"{p}/type") == "Battery" and _read(f"{p}/scope") != "Device":
                return f"battery {_read(f'{p}/capacity')}% {(_read(f'{p}/status') or '').lower()}".strip()
    except OSError:
        pass
    return ""


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def app_name(cmd):
    """A readable name from the command Steam started."""
    m = re.search(r"steamapps/common/([^/\"]+)/", cmd)
    if m:
        return m.group(1)
    if "deckhand" in cmd.lower() or "framesettings" in cmd.lower():
        return "Deckhand"
    m = re.search(r"\"?(/[^\"\s]+)", cmd)
    return os.path.basename(os.path.dirname(m.group(1))) if m else cmd[:60]


def ago(seconds):
    if seconds is None:
        return "?"
    return f"{seconds:.0f} s" if seconds < 90 else f"{seconds / 60:.0f} min" if seconds < 5400 else f"{seconds / 3600:.1f} h"


# --------------------------------------------------------------- watch ---
class Watch:
    def __init__(self, live=True):
        self.boot = debuglog.boot_id()
        self.counts = {"faults": 0, "recoveries": 0, "smmu": 0, "colour_cam": 0, "crashes": 0, "wakes": 0}
        self.last_fault, self.last_fault_text = None, ""
        self.last_recovery = None
        self.newest = 0.0            # time of the newest journal line handled
        self.cursor = None
        self.headset = None          # "on" / "off" (standby)
        self.csid = self.drops = 0   # camera link errors / dropped frames since the last status line
        self.recent = {}             # rate limit: kind of message -> [time logged, repeats not logged]
        self.leftovers = []          # SteamVR start: processes from its last run still running
        self.procs = None            # pid -> name at the last scan (None: not looked yet)
        self.live = live             # False while replaying a file (no /proc snapshots)
        self.started = time.time()
        if live:
            self.load_state()

    # ---- state (survives a restart of this service within one boot)
    def load_state(self):
        try:
            with open(STATE, encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, ValueError):
            return
        if isinstance(st, dict) and st.get("boot") == self.boot:
            self.cursor = st.get("cursor")
            self.counts.update({k: int(v) for k, v in (st.get("counts") or {}).items() if k in self.counts})
            self.last_fault, self.last_fault_text = st.get("last_fault"), st.get("last_fault_text", "")
            self.headset = st.get("headset")

    def save_state(self):
        if not self.live:
            return
        _write_json(STATE, {"boot": self.boot, "cursor": self.cursor, "counts": self.counts,
                            "last_fault": self.last_fault, "last_fault_text": self.last_fault_text,
                            "headset": self.headset})

    def write_status(self):
        if not self.live:
            return
        _write_json(debuglog.STATUS, {"boot": self.boot, "updated": time.time(), **self.counts,
                                      "last_fault": self.last_fault, "last_fault_text": self.last_fault_text,
                                      "headset": self.headset, "watching_since": self.started})

    # ---- writing
    def log(self, source, event, detail, t):
        debuglog.log(source, event, detail, t=t, force=True)

    def emit(self, source, event, detail, t, every=10.0):
        """Logs a kind of message at most once per `every` s; the repeats in between are counted
        and mentioned with the next one (or by flush_repeats)."""
        key = (source, event, re.sub(r"\d+", "N", detail)[:90])
        last = self.recent.get(key)
        if last and t - last[0] < every:
            last[1] += 1
            return
        if last and last[1]:
            detail += f" (+{last[1]} like it before)"
        self.recent[key] = [t, 0]
        self.log(source, event, detail, t)

    def flush_repeats(self, now):
        for key, (t, n) in list(self.recent.items()):
            if n and now - t > 30:
                self.log(key[0], key[1], f"(the line above repeated {n} more times)", now)
                self.recent[key] = [now, 0]
            elif not n and now - t > 600:
                del self.recent[key]

    def context(self):
        """What was going on: headset, SteamVR's process ages, what Deckhand runs, temperatures."""
        procs = scan()
        ages = {}
        for pid, name in procs.items():
            if name in ("SteamVR server", "tracking service", "SteamVR compositor"):
                ages[name] = proc_age(pid)
        deck = sorted({n for n in procs.values() if n.startswith("Deckhand")})
        parts = [f"headset {self.headset or '?'}",
                 "SteamVR: " + (", ".join(f"{n} up {ago(a)}" for n, a in sorted(ages.items())) or "not running"),
                 "Deckhand running: " + (", ".join(deck) or "nothing"),
                 temps_text(), battery()]
        return " | ".join(p for p in parts if p)

    def fault(self, what, msg, t, live):
        self.last_fault, self.last_fault_text = t, what
        detail = f"{what}: {msg}" + (f" | {self.context()}" if live else " | (from earlier in this boot)")
        self.log("camera", "FAULT", detail, t)
        self.write_status()

    # ---- journal entries
    def on_line(self, raw):
        if b'"console_log"' in raw and b"Game process" not in raw:
            return  # Steam's console is chatty: only app starts and ends matter here
        try:
            e = json.loads(raw)
        except ValueError:
            return
        self.cursor = e.get("__CURSOR", self.cursor)
        try:
            t = int(e.get("__REALTIME_TIMESTAMP")) / 1e6
        except (TypeError, ValueError):
            t = time.time()
        self.newest = max(self.newest, t)
        msg = e.get("MESSAGE")
        if isinstance(msg, list):
            msg = bytes(x & 0xFF for x in msg if isinstance(x, int)).decode("utf-8", "replace")
        msg = " ".join(str(msg or "").split())
        live = self.live and time.time() - t < 15
        ident = e.get("SYSLOG_IDENTIFIER", "")
        unit = e.get("_SYSTEMD_USER_UNIT", "")
        if e.get("_TRANSPORT") == "kernel":
            try:
                prio = int(e.get("PRIORITY", 6))
            except (TypeError, ValueError):
                prio = 6
            self.on_kernel(msg, t, live, prio)
        elif ident == "XRService":
            self.on_xrservice(msg, t, live)
        elif ident == "systemd-coredump":
            self.on_crash(msg, t, live)
        elif e.get("USER_UNIT") == "steamvr.service":
            self.on_steamvr_unit(msg, t)
        elif ident == "console_log":
            m = re.search(r"Game process (added|removed)\s*:\s*AppID (\d+) (.*)", msg)
            if m:
                self.log("steam", "app-start" if m.group(1) == "added" else "app-end",
                         f"{app_name(m.group(3))} (AppID {m.group(2)})", t)
        elif ident in ("timer_overlay", "ceiling_overlay", "space_tool"):
            self.emit("helper", ident, msg, t)
        elif unit == "frame-room-load.service":
            self.log("deckhand", "room-load", msg, t)
        elif unit == "frame-autooff.service":
            self.emit("deckhand", "power", msg, t)
        elif unit == "arcturus-camera-control.service":
            self.emit("arcturus", "camera-ctl", msg, t)
        elif ident == "v4l2cam":  # SteamVR's camera forwarder (passthrough for apps): its start, stop and trouble
            if "VR_Init" in msg or "VR_Shutdown" in msg or (XR_TROUBLE.search(msg) and not XR_NOISE.search(msg)):
                self.emit("camera", "v4l2cam", msg, t, every=30)

    def on_kernel(self, msg, t, live, prio=6):
        if "Unexpected CSID" in msg:  # constant on this hardware: only counted, as a rate
            self.csid += live
            return
        m = re.search(r"csid_isr: (\d+) callbacks suppressed", msg)
        if m:
            self.csid += int(m.group(1)) if live else 0
            return
        if "Unexpected ICP IRQ" in msg:
            self.counts["faults"] += 1
            self.fault("camera processor fault (ICP)", msg, t, live)
            return
        m = re.search(r"arm_smmu_context_fault: (\d+) callbacks suppressed", msg)
        if m:
            self.counts["smmu"] += int(m.group(1))
            return
        if "Unhandled context fault" in msg:
            self.counts["smmu"] += 1
            self.emit("camera", "smmu-fault", "memory-unit (SMMU) fault: " + msg, t, every=5)
            return
        if "arcimx" in msg:
            if "Accessory disconnected" in msg:
                self.counts["colour_cam"] += 1
                self.fault("colour camera disconnected", msg, t, live)
            else:
                self.emit("camera", "colour-cam", msg, t, every=5)
            return
        # Other camera driver lines and system trouble: warnings and worse only (boot chatter is info).
        trouble = prio <= 4 or re.search(r"drop|error|fail|timeout|timed out", msg, re.I)
        if ("qcom-camss" in msg or KERNEL_CAMERA.search(msg)) and trouble:
            self.emit("camera", "driver", msg, t, every=30)
        elif KERNEL_SYSTEM.search(msg) and prio <= 4:
            self.emit("system", "kernel", msg, t, every=30)

    def on_xrservice(self, msg, t, live=False):
        if "UserPresence] Received onLeaveStandby" in msg:
            self.counts["wakes"] += 1
            self.headset = "on"
            self.log("headset", "on", "headset put on (out of standby)", t)
            return
        if "UserPresence] Received onEnterStandby" in msg:
            self.headset = "off"
            self.log("headset", "off", "headset taken off (standby)", t)
            return
        if "Dropped a frame" in msg:
            self.drops += live
            return
        if "[ICP]" in msg:  # the camera processor, as the tracking service sees it
            if "Frame processing failure detected" in msg:
                self.counts["recoveries"] += 1
                self.last_recovery = t
                self.log("camera", "ICP-reset", "camera processor failed a frame; SteamVR resets it (hardware "
                                                "recovery)" + (f" | {self.context()}" if live else ""), t)
                self.write_status()
            else:
                self.emit("camera", "icp", msg, t, every=5)
            return
        for text, event in XR_EVENTS:
            if text in msg:
                self.emit("camera", event, msg, t, every=5)
                return
        if XR_TROUBLE.search(msg) and not XR_NOISE.search(msg):
            self.emit("tracking", "problem", msg, t, every=30)

    def on_crash(self, msg, t, live):
        m = re.search(r"Process (\d+) \(([^)]+)\) of user \d+ terminated abnormally with signal (\d+)/(\w+)", msg)
        if not m:
            return
        self.counts["crashes"] += 1
        name = m.group(2)
        detail = f"{FRIENDLY.get(name, name)} (pid {m.group(1)}) crashed: SIG{m.group(4)}"
        self.log("crash", name[:12], detail + (f" | {self.context()}" if live else ""), t)
        self.write_status()

    def on_steamvr_unit(self, msg, t):
        m = re.search(r"left-over process \d+ \(([^)]+)\)", msg)
        if m:
            self.leftovers.append(m.group(1))
            return
        if "unclean termination of a previous run" in msg:
            return  # systemd's comment on the left-over processes, which get one line of their own
        self.flush_leftovers(t)
        if msg.startswith("Stopping "):
            self.log("steamvr", "stopping", "SteamVR stopping", t)
        elif msg.startswith("Stopped "):
            self.log("steamvr", "stopped", "SteamVR stopped", t)
        elif msg.startswith("Started "):
            self.log("steamvr", "started", "SteamVR started", t)
        elif "Consumed" in msg or msg.startswith("Starting "):
            return
        else:
            self.emit("steamvr", "unit", msg, t)

    def flush_leftovers(self, t):
        if self.leftovers:
            names = ", ".join(FRIENDLY.get(n, n) for n in self.leftovers)
            self.log("steamvr", "leftovers", f"{len(self.leftovers)} processes of the previous SteamVR run were "
                                            f"still running when it started again: {names}", t)
            self.leftovers = []

    # ---- every few seconds
    def check_processes(self):
        now = time.time()
        procs = scan()
        if self.procs is None:  # first look: what is running already (with how long it has been up)
            running = ", ".join(f"{name} (up {ago(proc_age(pid))})" for pid, name in sorted(procs.items()))
            self.log("watch", "running", "running now: " + (running or "no SteamVR or Deckhand processes"), now)
            self.procs = procs
            return
        for pid, name in procs.items():
            if pid not in self.procs:
                src = "deckhand" if name.startswith("Deckhand") else "steamvr"
                self.log(src, "process", f"{name} started (pid {pid})", now)
        for pid, name in self.procs.items():
            if pid not in procs:
                src = "deckhand" if name.startswith("Deckhand") else "steamvr"
                self.log(src, "process", f"{name} ended (pid {pid})", now)
        self.procs = procs

    def status_line(self, minutes):
        deck = sorted({n for n in (self.procs or {}).values() if n.startswith("Deckhand")})
        c = self.counts
        parts = [temps_text(), battery(),
                 f"camera link errors {self.csid / max(minutes, 0.1):.0f}/min, dropped frames {self.drops}",
                 f"this boot: {c['faults']} camera faults, {c['recoveries']} camera resets, {c['crashes']} crashes, "
                 f"put on {c['wakes']} times",
                 "Deckhand running: " + (", ".join(deck) or "nothing")]
        self.log("status", "minute" if minutes <= 1.5 else f"{minutes:.0f} min", " | ".join(p for p in parts if p),
                 time.time())
        self.csid = self.drops = 0


def _write_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError:
        pass


def journal_cmd(cursor):
    cmd = ["journalctl", "-f", "--no-tail", "-o", "json", "--no-pager", "-b"]
    if cursor:
        cmd += ["--after-cursor", cursor]
    for i, m in enumerate(MATCHES):
        cmd += (["+"] if i else []) + [m]
    return cmd


def run_service():
    w = Watch()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: stop.set())
    q = queue.Queue(maxsize=5000)
    fresh = w.cursor is None
    w.log("watch", "start", "camera watch started" + (", reading this boot's camera history first"
                                                      if fresh else ", continuing where it stopped"), time.time())

    def reader():
        delay = 5
        while not stop.is_set():
            while not q.empty() and not stop.is_set():  # resume from the last line actually handled
                time.sleep(0.2)
            began = time.monotonic()
            try:
                p = subprocess.Popen(journal_cmd(w.cursor), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except OSError as e:
                w.log("watch", "journal", f"can't read the journal: {e}", time.time())
                stop.wait(60)
                continue
            for raw in p.stdout:
                q.put(raw)
                if stop.is_set():
                    break
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
            if stop.is_set():
                break
            delay = 5 if time.monotonic() - began > 300 else min(delay * 2, 300)
            w.log("watch", "journal", f"journal reader stopped (exit {p.returncode}); again in {delay} s", time.time())
            stop.wait(delay)

    threading.Thread(target=reader, daemon=True).start()
    next_scan = next_status = next_save = 0.0
    last_status = quiet_since = time.monotonic()
    caught_up = not fresh
    while not stop.is_set():
        try:
            raw = q.get(timeout=1)
        except queue.Empty:
            raw = None
        if raw:
            try:
                w.on_line(raw)
            except Exception as e:  # noqa: BLE001 (one odd line must not stop the watch)
                w.emit("watch", "error", f"{type(e).__name__}: {e}", time.time(), every=300)
            quiet_since = time.monotonic()
        now = time.monotonic()
        if not caught_up and (time.time() - w.newest < 15 or now - quiet_since > 3):
            caught_up = True
            w.log("watch", "caught-up", f"history read: this boot so far {w.counts['faults']} camera faults, "
                                        f"{w.counts['recoveries']} camera resets, {w.counts['crashes']} crashes, "
                                        f"put on {w.counts['wakes']} times", time.time())
            w.write_status()
        if now >= next_scan and caught_up:
            next_scan = now + 5
            w.check_processes()
            w.flush_leftovers(time.time())
        every = 60 if w.headset == "on" else 600
        if now >= next_status and caught_up:
            if next_status:
                w.status_line((now - last_status) / 60)
                w.flush_repeats(time.time())
                debuglog.rotate()
            last_status, next_status = now, now + every
            w.write_status()
        elif w.headset == "on" and next_status - now > 60:
            next_status = now + 60  # put on: report every minute from now
        if now >= next_save:
            next_save = now + 30
            w.save_state()
    w.save_state()
    w.write_status()
    w.log("watch", "stop", "camera watch stopped", time.time())


# --------------------------------------------------------------- service ---
def _systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=30)


def ensure_service():
    """Runs the watch while the debug log is on, else stops it. Returns (ok, message)."""
    if not debuglog.enabled():
        if os.path.exists(UNIT_PATH):
            _systemctl("disable", "--now", UNIT)
        return True, "off"
    try:
        with open(UNIT_PATH, encoding="utf-8") as f:
            current = f.read()
    except OSError:
        current = None
    if current != UNIT_TEXT:
        os.makedirs(os.path.dirname(UNIT_PATH), exist_ok=True)
        tmp = UNIT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(UNIT_TEXT)
        os.replace(tmp, UNIT_PATH)
        _systemctl("daemon-reload")
        if current is not None:
            _systemctl("try-restart", UNIT)  # it runs this copy's program now
    r = _systemctl("enable", "--now", UNIT)
    return r.returncode == 0, ((r.stderr or r.stdout).strip() or "on") if r.returncode else "on"


def service_active():
    return _systemctl("is-active", "--quiet", UNIT).returncode == 0


def replay(path, out=None):
    """Feeds saved `journalctl -o json` output through the filters into `out` (testing)."""
    if out:
        debuglog.PATH = out
    w = Watch(live=False)
    with open(path, "rb") as f:
        for raw in f:
            w.on_line(raw)
    w.flush_leftovers(time.time())
    return w


if __name__ == "__main__":
    if sys.argv[1:] == ["--service"]:
        run_service()
    elif sys.argv[1:] == ["--status"]:
        print(json.dumps(debuglog.status(), indent=2))
    elif len(sys.argv) in (3, 4) and sys.argv[1] == "--replay":
        w = replay(sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else None)
        print(json.dumps(w.counts))
    else:
        print(__doc__)
