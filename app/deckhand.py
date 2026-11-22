#!/usr/bin/env python3
"""Deckhand: a small settings app for the Steam Frame.

Full-screen, phone-style pages (big text and buttons for VR):
- Fan: live fan speed, temperatures and battery; presets (Stock, Quiet,
  Extra quiet) and a custom minimum speed + strength. Changing it calls the
  root helper installed by setup.sh.
- Power: shut down now, sleep timer, auto shut-off after standby (power.py).
- Comfort: SteamVR Do Not Disturb, screens-off timer, theater surroundings
  brightness, screenshots.
- Recline: Recline VR status, recline / back-to-normal buttons.
- System: battery, network address, restart SteamVR.

Uses only what SteamOS ships: Python 3 and GTK 3 (through PyGObject).
Run it from the Steam library (works in VR), or with --snapshot PREFIX to
render every page off-screen into PREFIX-<n>.png (for testing).
"""
import hashlib
import html
import json
import math
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, GObject, Gtk, Pango  # noqa: E402

# Window class "deckhand" (instead of "frame_settings.py") so taskbars match
# the window to frame-settings.desktop (StartupWMClass) and show its icon.
GLib.set_prgname("deckhand")
GLib.set_application_name("Deckhand")
Gdk.set_program_class("deckhand")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths  # noqa: E402  (install folder layout)
ROOT = paths.ROOT
APP_ICON = paths.ICON
import power  # noqa: E402  (same folder: shutdown, sleep timer, auto shut-off)
import kitchen_timer  # noqa: E402  (countdown popup in the headset)
import rooms  # noqa: E402  (saved play areas, experimental)
import reminders  # noqa: E402  (reminders at a time and date)
import wifi_map  # noqa: E402  (Wi-Fi signal map of the play space)
import ceiling  # noqa: E402  (ceiling indicator)
import house_map  # noqa: E402  (whole-house Wi-Fi walk)
import updater  # noqa: E402  (Update app: newest version from GitHub)
import datetime  # noqa: E402
import struct  # noqa: E402

HELPER = "/etc/frame-settings/fanmode"
HELPER_MIN_VERSION = 3
STOCK_CFG = "/usr/share/deckard-fan-control/deckard-config.yaml"
QUIET_CFG = "/etc/deckard-fan-control-quiet/deckard-config.yaml"
QUIET_DROPIN = "/etc/systemd/system/deckard-fan-control.service.d/quiet.conf"
FAN_LOG = "/var/log/deckard-fan-control.log"
FAN_HWMON_NAME = "slg4ax46073v"
OPENVR_PATHS = os.path.expanduser("~/.config/openvr/openvrpaths.vrpath")
RECLINECTL = os.path.expanduser("~/.local/share/recline-vr/bin/linuxarm64/reclinectl")
FLOOR_MAX = 55
SHOT_TOOLTIP = (
    "<b>How to take a screenshot</b>\n"
    "Hold the <b>Steam button</b> on either controller and pull a <b>trigger</b>\n"
    "(either trigger works).\n\n"
    "<b>Where they go</b>\n"
    "Steam on the headset: <b>Media</b>.\n"
    "Files: ~/.local/share/Steam/userdata/&lt;id&gt;/760/remote/250820/screenshots\n\n"
    "Turn <b>Disable screenshots</b> on to block them, e.g. if you keep taking them by accident.")
PRESETS = {  # name: (floor, charging floor, strength %) ; None = Valve stock
    "Stock": None,
    "Quiet": (0, 0, 100),
    "Extra quiet": (0, 0, 70),
}


def read(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return default


def run(cmd, timeout=10, env=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def vr_tool_env():
    """Environment for SteamVR command-line tools (see paths.clean_env)."""
    return paths.clean_env()


# ---------------------------------------------------------------- fan ---
_HWMON = None


def fan_hwmon():
    """The fan's hwmon folder (looked up once; again only if it disappears)."""
    global _HWMON
    if _HWMON and os.path.isdir(_HWMON):
        return _HWMON
    base = "/sys/class/hwmon"
    try:
        for name in os.listdir(base):
            if read(os.path.join(base, name, "name")) == FAN_HWMON_NAME:
                _HWMON = os.path.join(base, name)
                return _HWMON
    except OSError:
        pass
    return None


def fan_controller_outdated():
    """True when a custom fan setting runs a copy of Valve's fan controller that a SteamOS update has
    since replaced (applying the setting again copies the new one)."""
    if not os.path.exists(QUIET_DROPIN):
        return False
    for name in ("fancontrol.py", "PID.py"):
        try:
            with open(os.path.join("/usr/share/deckard-fan-control", name), "rb") as a, \
                    open(os.path.join(os.path.dirname(QUIET_CFG), name), "rb") as b:
                if a.read() != b.read():
                    return True
        except OSError:
            continue
    return False


def fan_mode():
    """('stock'|'custom', floor, charging_floor, strength)"""
    if os.path.exists(QUIET_DROPIN):
        text = read(QUIET_CFG, "") or ""
        tag = re.search(r"frame-settings: floor=(\d+) charging=(\d+) strength=(\d+)", text)
        if tag:
            return ("custom", *map(int, tag.groups()))
        floor = re.search(r"^fan_min_speed:\s*(\d+)", text, re.M)
        chg = re.search(r"^fan_charging_min_speed:\s*(\d+)", text, re.M)
        return "custom", int(floor.group(1)) if floor else 0, int(chg.group(1)) if chg else 0, 100
    text = read(STOCK_CFG, "") or ""
    floor = re.search(r"^fan_min_speed:\s*(\d+)", text, re.M)
    chg = re.search(r"^fan_charging_min_speed:\s*(\d+)", text, re.M)
    return "stock", int(floor.group(1)) if floor else 30, int(chg.group(1)) if chg else 40, 100


def battery():
    """(percent or None, charging bool) of the headset's own battery. Batteries of peripherals
    (scope "Device", e.g. controllers) are ignored."""
    try:
        names = sorted(os.listdir("/sys/class/power_supply"))
    except OSError:
        return None, False
    for n in names:
        p = f"/sys/class/power_supply/{n}"
        if read(f"{p}/type") != "Battery" or read(f"{p}/scope") == "Device":
            continue
        cap = read(f"{p}/capacity", "")
        return (int(cap) if cap.isdigit() else None), read(f"{p}/status") == "Charging"
    return None, False


def fan_log_temps():
    """Hottest CPU and GPU temperature from the last line of the fan service log."""
    try:
        with open(FAN_LOG, "rb") as f:
            first = f.readline().decode("utf-8", "replace").strip()  # CSV header
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 4096))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None, None
    if not first.startswith("TIMESTAMP"):
        return None, None
    data = next((l for l in reversed(lines) if l and l[0].isdigit() and l.count(",") == first.count(",")), None)
    if not data:
        return None, None
    row = dict(zip(first.split(","), data.split(",")))

    def hottest(prefix):
        vals = []
        for k, v in row.items():
            if re.fullmatch(prefix + r"\d+_TEMP", k):
                try:
                    vals.append(float(v))
                except ValueError:
                    pass
        return max(vals) if vals else None
    return hottest("C"), hottest("G")


def fan_status(temps=True):
    """Fan speed, battery and (temps=True: from the fan log) the hottest CPU / GPU temperature."""
    h = fan_hwmon()
    rpm = read(os.path.join(h, "fan1_input")) if h else None
    cpu, gpu = fan_log_temps() if temps else (None, None)
    pct, chg = battery()
    return {"rpm": int(rpm) if rpm and rpm.isdigit() else None,
            "cpu": cpu, "gpu": gpu, "battery": pct, "charging": chg}


def helper_version():
    """Helper version if sudo lets us run it without a password, else 0."""
    if not (os.path.isfile(HELPER) and os.access(HELPER, os.X_OK)):
        return 0
    rc, out = run(["sudo", "-n", HELPER, "check"])
    if rc != 0:
        return 0
    m = re.search(r"ok\s*(\d+)?", out)
    return int(m.group(1) or 1) if m else 0


def run_helper(args):
    rc, out = run(["sudo", "-n", HELPER, *args], timeout=30)
    if rc != 0 and "password" in out.lower():
        out = "setup isn't finished: press Run setup now."
    return rc == 0, out


# ----------------------------------------------------- SteamVR settings ---
def openvr_paths():
    try:
        with open(OPENVR_PATHS, encoding="utf-8") as f:
            d = json.load(f)
        return (d.get("runtime") or ["/opt/steamvr"])[0], (d.get("config") or [None])[0]
    except (OSError, ValueError):
        return "/opt/steamvr", os.path.expanduser("~/.config/openvr/config")


def vrcmd_path():
    runtime, _ = openvr_paths()
    for arch in ("linuxarm64", "linux64"):
        p = os.path.join(runtime, "bin", arch, "vrcmd")
        if os.access(p, os.X_OK):
            return p
    return None


def vrsettings_path():
    _, cfg = openvr_paths()
    return os.path.join(cfg, "steamvr.vrsettings") if cfg else None


def steamvr_running():
    return run(["pgrep", "-x", "vrserver"])[0] == 0


_DEFAULTS = None


def vr_defaults():
    """SteamVR's built-in defaults (default.vrsettings, which has // comments)."""
    global _DEFAULTS
    if _DEFAULTS is None:
        runtime, _ = openvr_paths()
        try:
            with open(os.path.join(runtime, "resources", "settings", "default.vrsettings"), encoding="utf-8") as f:
                _DEFAULTS = json.loads(re.sub(r"(?m)^\s*//.*$", "", f.read()))
        except (OSError, ValueError):
            _DEFAULTS = {}
    return _DEFAULTS


def vr_get(section, key, kind, default):
    """kind: 'bool' | 'int' | 'float'. Reads SteamVR's settings file (SteamVR
    saves every change to it right away), falling back to SteamVR's defaults.
    No SteamVR connection, so it is instant and can't disturb anything."""
    conv = {"bool": bool, "int": int, "float": float}[kind]
    try:
        with open(vrsettings_path(), encoding="utf-8") as f:
            user = json.load(f).get(section, {})
        if key in user:
            return conv(user[key])
    except (OSError, ValueError, TypeError):
        pass
    try:
        return conv(vr_defaults().get(section, {}).get(key, default))
    except (ValueError, TypeError):
        return default


def vr_set(section, key, kind, value):
    if steamvr_running() and vrcmd_path():
        # vrcmd parses values as numbers: a bool must be sent as 1/0 ("true" would become 0).
        arg = ("1" if value else "0") if kind == "bool" else str(value)
        run([vrcmd_path(), f"--set-settings-{kind}", f"{section}.{key}", arg], env=vr_tool_env())
        for _ in range(10):  # SteamVR writes the file a moment later
            if vr_get(section, key, kind, None) == value if kind != "float" else \
                    abs((vr_get(section, key, kind, -1.0)) - float(value)) < 1e-3:
                return True
            time.sleep(0.1)
    else:
        path = vrsettings_path()
        if not path:
            return False
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError):
            return False  # unreadable or half-written: never replace the user's whole SteamVR settings
        if not isinstance(data, dict) or not isinstance(data.get(section, {}), dict):
            return False
        data.setdefault(section, {})[key] = {"bool": bool, "int": int, "float": float}[kind](value)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=3)
        os.replace(tmp, path)
    got = vr_get(section, key, kind, None)
    return got == value if kind != "float" else got is not None and abs(got - float(value)) < 1e-3


# ------------------------------------------------------------- recline ---
def recline_available():
    return os.access(RECLINECTL, os.X_OK)


def recline_status():
    if not recline_available():
        return "Recline VR is not installed."
    if not steamvr_running():
        return "SteamVR is not running."
    rc, out = run([RECLINECTL, "status"], env=vr_tool_env())
    loaded = re.search(r"status:\s*(.+)", out)
    rot = re.search(r"^R:\s*yaw\s*(-?[\d.]+)\s*pitch\s*(-?[\d.]+)\s*roll\s*(-?[\d.]+)", out, re.M)
    lines = [f"Driver: {loaded.group(1).replace('Driver: ', '')}" if loaded else "Driver: unknown"]
    if rot:
        pitch, roll = abs(float(rot.group(2))), abs(float(rot.group(3)))
        lines.append(f"Reclined (pitch {pitch:.0f}°, roll {roll:.0f}°)" if max(pitch, roll) > 1 else "Normal (not reclined)")
    return "\n".join(lines)


# -------------------------------------------------------------- system ---
def ip_addresses():
    rc, out = run(["ip", "-4", "-o", "addr", "show", "scope", "global"])
    addrs = re.findall(r"^\d+:\s+(\S+)\s+inet\s+([\d.]+)", out, re.M)
    return [f"{ip}  ({dev})" for dev, ip in addrs] or ["no network"]


# ----------------------------------------------------------------- UI ---
# Big, phone-style layout for reading and pointing in VR: full screen,
# side navigation, cards, large buttons and switches, no dropdowns or pop-ups.
from gi.repository import GdkPixbuf  # noqa: E402

ACCENT = "#66ccff"
CSS = f"""
window, .root {{ background: #141b24; color: #eef4fa; font-size: 16pt; }}
.rail {{ background: #0e141b; padding: 22px 16px; }}
.app-title {{ font-size: 21pt; font-weight: bold; }}
.rail-status {{ font-size: 13pt; color: #9fb3c8; }}
.nav {{ border-radius: 18px; padding: 4px 18px; min-height: 54px; background: transparent;
        border: none; box-shadow: none; color: #c9d6e3; }}
.nav:hover {{ background: #1d2733; }}
.nav.active {{ background: #22384d; color: #ffffff; }}
.nav label {{ font-size: 18pt; font-weight: bold; }}
.page {{ padding: 30px 34px; }}
.page-title {{ font-size: 26pt; font-weight: bold; }}
.card {{ background: #1f2a36; border-radius: 22px; padding: 26px 28px; }}
.card-title {{ font-size: 19pt; font-weight: bold; }}
.desc {{ font-size: 13pt; color: #a9bccd; }}
.big {{ font-size: 40pt; font-weight: bold; }}
.status {{ font-size: 15pt; color: #c9d6e3; }}
.ok {{ color: #5fd68a; }}
.err {{ color: #ff7b72; }}
button {{ min-height: 64px; border-radius: 16px; padding: 8px 22px; font-size: 16pt;
          background: #2c3b4b; color: #eef4fa; border: 2px solid transparent; box-shadow: none; }}
button:hover {{ background: #36495d; }}
button.primary {{ background: #2a6f97; }}
button.primary:hover {{ background: #3282b0; }}
button.danger {{ background: #8a2f2f; font-size: 19pt; min-height: 92px; }}
button.danger:hover {{ background: #a33939; }}
.big-cmd {{ font-family: monospace; font-size: 20pt; font-weight: bold; color: #fde725; }}
progressbar.hold trough {{ min-height: 16px; border-radius: 8px; background: #2c3b4b; }}
progressbar.hold progress {{ min-height: 16px; border-radius: 8px; background: #f2a33a; }}
button.armed {{ background: #d04040; }}
button.selected {{ background: #22384d; border-color: {ACCENT}; color: #ffffff; }}
button.tile {{ padding: 16px 22px; min-height: 130px; }}
.tile-name {{ font-size: 20pt; font-weight: bold; }}
.tile-desc {{ font-size: 13pt; color: #b9cad9; }}
button.step {{ min-width: 72px; font-size: 22pt; font-weight: bold; padding: 4px; }}
button.seg {{ padding: 8px 2px; min-width: 0; }}
switch {{ min-width: 116px; min-height: 58px; border-radius: 29px; }}
switch slider {{ min-width: 54px; min-height: 54px; border-radius: 27px; }}
scale trough {{ min-height: 14px; border-radius: 7px; }}
scale slider {{ min-width: 46px; min-height: 46px; border-radius: 23px; }}
scale value {{ font-size: 17pt; }}
entry {{ font-size: 13pt; min-height: 52px; }}
tooltip, tooltip * {{ font-size: 14pt; }}
.hud {{ background: #0f1620; border: 5px solid {ACCENT}; padding: 10px 20px; }}
.hud-name {{ font-size: 46px; font-weight: bold; color: #9fd8ff; }}
.hud-time {{ font-size: 132px; font-weight: bold; color: #ffffff; }}
.hud-done {{ font-size: 90px; font-weight: bold; color: #ffcf5c; }}
.hud-note {{ font-size: 60px; font-weight: bold; color: #ffcf5c; }}
.spin-value {{ font-size: 26pt; font-weight: bold; }}
""".encode()


_CSS_LOADED = False


def ensure_css():
    global _CSS_LOADED
    if not _CSS_LOADED:
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        _CSS_LOADED = True


def label(text="", css=None, wrap=False, markup=False, selectable=False, xalign=0.0):
    lab = Gtk.Label(xalign=xalign)
    lab.set_markup(text) if markup else lab.set_text(text)
    lab.set_line_wrap(wrap)
    if wrap:  # break long words too, so a long name can never push the window past the screen
        lab.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
    lab.set_selectable(selectable)
    for c in (css or "").split():
        lab.get_style_context().add_class(c)
    return lab


def add_class(widget, *classes):
    for c in classes:
        widget.get_style_context().add_class(c)
    return widget


def card(title=None, desc=None):
    box = add_class(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16), "card")
    if title:
        box.pack_start(label(title, "card-title"), False, False, 0)
    if desc:
        box.pack_start(label(desc, "desc", wrap=True), False, False, 0)
    return box


# Icons the desktop theme may not have, drawn here (light lines for the dark buttons)
ICON_SVG = {
    "deckhand-move": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#eef4fa" '
                     'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v20M2 12h20"/>'
                     '<path d="M9 5l3-3 3 3M9 19l3 3 3-3M5 9l-3 3 3 3M19 9l3 3-3 3"/></svg>',
}


def icon_image(names, size=48):
    if isinstance(names, str) and names.endswith(".png") and os.path.isfile(names):
        pix = GdkPixbuf.Pixbuf.new_from_file_at_size(names, size, size)
        return Gtk.Image.new_from_pixbuf(pix)
    for n in ([names] if isinstance(names, str) else names):
        if n in ICON_SVG:
            loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
            loader.set_size(size, size)
            loader.write(ICON_SVG[n].encode())
            loader.close()
            return Gtk.Image.new_from_pixbuf(loader.get_pixbuf())
    theme = Gtk.IconTheme.get_default()
    for n in ([names] if isinstance(names, str) else names):
        if theme.has_icon(n):
            img = Gtk.Image.new_from_icon_name(n, Gtk.IconSize.DIALOG)
            img.set_pixel_size(size)
            return img
    return Gtk.Box()


def set_result(lab, text, ok):
    ctx = lab.get_style_context()
    ctx.remove_class("ok")
    ctx.remove_class("err")
    ctx.add_class("ok" if ok else "err")
    lab.set_text(text)


def set_info(lab, text):
    """Plain (neither good nor bad) text on a label that set_result() may have coloured before."""
    ctx = lab.get_style_context()
    ctx.remove_class("ok")
    ctx.remove_class("err")
    lab.set_text(text)


def safe(fn):
    """Wraps a GLib timeout/idle callback: an exception is printed instead of silently removing the
    callback (PyGObject drops a callback that raises, which froze the live readouts for good)."""
    def run_safely(*args):
        try:
            return fn(*args)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return True
    return run_safely


def in_background(work, done):
    """Runs work() off the UI thread, then done(result) on it."""
    def runner():
        try:
            result = work()
        except Exception as e:  # noqa: BLE001
            result = e
        GLib.idle_add(lambda: (done(result), False)[1])
    threading.Thread(target=runner, daemon=True).start()


class SerialJobs:
    """One background thread running jobs in the order given (systemd changes that must not overlap
    or finish out of order), then done(result) on the UI thread, like in_background()."""

    def __init__(self):
        self.q = queue.Queue()
        threading.Thread(target=self._loop, daemon=True).start()

    def put(self, work, done=None):
        self.q.put((work, done))

    def _loop(self):
        while True:
            work, done = self.q.get()
            try:
                result = work()
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                result = e
            if done:
                GLib.idle_add(lambda d=done, r=result: (d(r), False)[1])


def user_systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=30)


def dbm_text(s):
    """A reading's strength as a short number ("-52"), or "--" without one."""
    return f"{s['dbm']:.0f}" if s and s.get("dbm") is not None else "--"


def tracking_note(rec):
    """What a mapping progress line adds when positions aren't coming in."""
    if rec.pose:
        return ""
    return " · tracking stopped: is SteamVR running?" if rec.ended else " · headset not tracking: put it on"


def switch_row(title, desc_label, switch):
    """Title + description on the left, a big switch on the right."""
    row = Gtk.Box(spacing=20)
    texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    texts.pack_start(label(title, "card-title"), False, False, 0)
    texts.pack_start(desc_label, False, False, 0)
    row.pack_start(texts, True, True, 0)
    switch.set_valign(Gtk.Align.CENTER)
    row.pack_end(switch, False, False, 0)
    return row


def write_rgba(pix, path):
    """Saves a pixbuf as raw RGBA with an 8-byte (width, height) header, for timer_overlay."""
    if not pix.get_has_alpha():
        pix = pix.add_alpha(False, 0, 0, 0)
    w, h, stride = pix.get_width(), pix.get_height(), pix.get_rowstride()
    data = pix.get_pixels()
    rows = b"".join(data[y * stride:y * stride + w * 4] for y in range(h))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(struct.pack("<II", w, h) + rows)
    os.replace(tmp, path)


class Segmented(Gtk.Box):
    """Rows of big buttons where one is selected (replaces dropdowns). per_row splits long lists."""

    def __init__(self, choices, on_pick, per_row=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.choices = list(choices)  # [(value, text)]
        self.on_pick = on_pick
        self.buttons = []
        per_row = per_row or len(self.choices)
        row = None
        for i, (value, text) in enumerate(self.choices):
            if i % per_row == 0:
                row = Gtk.Box(spacing=10, homogeneous=True)
                self.pack_start(row, False, False, 0)
            b = add_class(Gtk.Button(label=text), "seg")
            b.connect("clicked", self._clicked, value)
            row.pack_start(b, True, True, 0)
            self.buttons.append(b)
        for _ in range(-len(self.choices) % per_row):  # keep the last row's buttons the same width
            row.pack_start(Gtk.Box(), True, True, 0)
        self.value = None

    def set_value(self, value):
        self.value = value
        for (v, _), b in zip(self.choices, self.buttons):
            ctx = b.get_style_context()
            (ctx.add_class if v == value else ctx.remove_class)("selected")

    def _clicked(self, _b, value):
        self.set_value(value)
        self.on_pick(value)


class Stepper(Gtk.Box):
    """[−] slider [+]: big step buttons because dragging is fiddly in VR."""

    def __init__(self, adj, step, marks=None, on_change=None):
        super().__init__(spacing=12)
        self.adj, self.step, self.on_change = adj, step, on_change
        minus = add_class(Gtk.Button(label="−"), "step")
        plus = add_class(Gtk.Button(label="+"), "step")
        minus.connect("clicked", lambda *_: self.bump(-1))
        plus.connect("clicked", lambda *_: self.bump(+1))
        self.scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=adj, hexpand=True)
        self.scale.set_digits(0)
        self.scale.set_draw_value(False)  # shown in its own label, so the knob never covers it
        for v, t in (marks or {}).items():
            self.scale.add_mark(v, Gtk.PositionType.BOTTOM, t)
        if on_change:
            self.scale.connect("button-release-event", lambda *_: (on_change(), False)[1])
        # A scroll over the slider would change it without applying it: let the page scroll instead.
        self.scale.connect("scroll-event", self._pass_scroll)
        self.value_lbl = label("", "card-title", xalign=1.0)
        self.value_lbl.set_width_chars(4)
        adj.connect("value-changed", lambda a: self.value_lbl.set_text(f"{a.get_value():.0f}"))
        self.value_lbl.set_text(f"{adj.get_value():.0f}")
        self.pack_start(minus, False, False, 0)
        self.pack_start(self.scale, True, True, 0)
        self.pack_start(self.value_lbl, False, False, 0)
        self.pack_start(plus, False, False, 0)

    def bump(self, direction):
        self.adj.set_value(self.adj.get_value() + direction * self.step)
        if self.on_change:
            self.on_change()

    @staticmethod
    def _pass_scroll(scale, _event):
        GObject.signal_stop_emission_by_name(scale, "scroll-event")
        return False


class Spin(Gtk.Box):
    """[−] value [+] with a big value in the middle; wraps around (hours, minutes, days)."""

    def __init__(self, lo, hi, step, value, fmt=str, on_change=None, wrap=True, width_chars=3):
        super().__init__(spacing=10)
        self.lo, self.hi, self.step, self.value, self.fmt, self.wrap = lo, hi, step, value, fmt, wrap
        self.on_change = on_change
        minus = add_class(Gtk.Button(label="−"), "step")
        plus = add_class(Gtk.Button(label="+"), "step")
        minus.connect("clicked", lambda *_: self.bump(-1))
        plus.connect("clicked", lambda *_: self.bump(+1))
        self.lbl = label("", "spin-value", xalign=0.5)
        self.lbl.set_width_chars(width_chars)
        self.pack_start(minus, False, False, 0)
        self.pack_start(self.lbl, True, True, 0)
        self.pack_start(plus, False, False, 0)
        self.set(value)

    def set(self, value, notify=False):
        self.value = value
        self.lbl.set_text(self.fmt(value))
        if notify and self.on_change:
            self.on_change(value)

    def bump(self, direction):
        v = self.value + direction * self.step
        if v > self.hi:
            v = self.lo if self.wrap else self.hi
        elif v < self.lo:
            v = (self.hi - (self.hi - self.lo) % self.step) if self.wrap else self.lo
        self.set(v, notify=True)


class ConfirmButton(Gtk.Button):
    """Tap once to arm, tap again within 5 s to do it (no small pop-up dialogs). The text wraps,
    so the longer "tap again" text can never make the window wider than the screen."""

    def __init__(self, text, armed_text, action, css="danger"):
        super().__init__()
        self.text, self.armed_text, self.action = text, armed_text, action
        self.lbl = label(text, wrap=True, xalign=0.5)
        self.lbl.set_justify(Gtk.Justification.CENTER)
        self.add(self.lbl)
        add_class(self, css)
        self.armed_until = 0.0
        self._timer = None
        self.connect("clicked", self._clicked)

    def set_text(self, text):
        self.text = text
        if not self.armed_until:
            self.lbl.set_text(text)

    def _clicked(self, *_):
        if time.monotonic() < self.armed_until:
            self.disarm()
            self.action()
            return
        self.armed_until = time.monotonic() + 5
        self.lbl.set_text(self.armed_text)
        add_class(self, "armed")
        if self._timer:
            GLib.source_remove(self._timer)
        self._timer = GLib.timeout_add(5000, self._expired)

    def _expired(self):
        self._timer = None  # this timer ends by returning False
        return self.disarm()

    def disarm(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = None
        self.armed_until = 0.0
        self.lbl.set_text(self.text)
        self.get_style_context().remove_class("armed")
        return False


class HoldButton(Gtk.Box):
    """Press and HOLD for `seconds` to do it: a bar fills and the label counts down; letting go (or
    moving off the button) before the end cancels. For things that can't be undone."""

    def __init__(self, text, action, seconds=5.0, css="danger"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.text, self.action, self.seconds = text, action, seconds
        self.button = add_class(Gtk.Button(label=text), css)
        self.bar = add_class(Gtk.ProgressBar(), "hold")
        self.pack_start(self.button, False, False, 0)
        self.pack_start(self.bar, False, False, 0)
        self.started = None
        self.button.connect("button-press-event", self._press)
        self.button.connect("button-release-event", self._cancel)
        self.button.connect("leave-notify-event", self._cancel)

    def _press(self, _w, event):
        if event.button == 1 and self.started is None:
            self.started = time.monotonic()
            GLib.timeout_add(50, self._tick)
        return False

    def _tick(self):
        if self.started is None:
            return False
        held = time.monotonic() - self.started
        self.bar.set_fraction(min(1.0, held / self.seconds))
        if held >= self.seconds:
            self.started = None
            self.button.set_label("Done")
            self.action()
            GLib.timeout_add(2500, self._reset)
            return False
        self.button.set_label(f"Keep holding… {self.seconds - held:.1f} s")
        return True

    def _cancel(self, *_):
        if self.started is not None:
            self.started = None
            self.bar.set_fraction(0)
            self.button.set_label(self.text)
        return False

    def _reset(self):
        if self.started is None:
            self.bar.set_fraction(0)
            self.button.set_label(self.text)
        return False


class HudRenderer:
    """Draws the headset popup pictures (pixel sizes, not DPI) as raw RGBA files for
    timer_overlay: 8-byte header (uint32 width, height, little endian) + pixels."""

    def __enter__(self):
        ensure_css()  # the popup's look, even when no app window exists yet
        self.off = Gtk.OffscreenWindow()
        self.hud = add_class(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0), "hud")
        self.hud.set_size_request(640, 230)
        self.name = label("", "hud-name", xalign=0.5)
        self.name.set_ellipsize(Pango.EllipsizeMode.END)  # long names: "Lasagna in the ov…", popup stays 640 px
        self.name.set_max_width_chars(22)
        self.big = None
        self.style = None
        self.last = None
        self.hud.pack_start(self.name, False, False, 0)
        self.off.add(self.hud)
        self.off.show_all()
        return self

    def __exit__(self, *_):
        self.off.destroy()

    def _settle(self, rounds=1):
        for _ in range(rounds):
            if rounds > 1:
                self.off.queue_resize()
            self.off.queue_draw()
            while Gtk.events_pending():
                Gtk.main_iteration_do(False)
            if self.off.get_window():
                self.off.get_window().process_updates(True)
            if rounds > 1:
                time.sleep(0.01)

    def render(self, title, big, path, done=False):
        if self.last == (title, big, path, done) and os.path.exists(path):
            return  # same picture as last time: nothing to draw or write
        self.last = (title, big, path, done)
        self.name.set_text(title)
        long_text = len(big) > 10  # a reminder's own words: smaller and wrapped
        style = "hud-note" if long_text else "hud-done" if done else "hud-time"
        new_label = style != self.style
        if new_label:
            # A new label per style: GTK keeps the old text size when a label's style class changes.
            if self.big:
                self.hud.remove(self.big)
            self.big = label("", style, xalign=0.5, wrap=long_text)
            self.big.set_justify(Gtk.Justification.CENTER)
            self.hud.pack_start(self.big, True, True, 0)
            self.big.show()
            self.style = style
        self.big.set_text(big)
        # The offscreen window repaints one step late: settle, grab once to flush, then grab for real.
        # A freshly added label also needs a few layout passes first.
        self._settle(8 if new_label else 1)
        self.off.get_pixbuf()
        self._settle(8 if new_label else 1)
        write_rgba(self.off.get_pixbuf(), path)


class FrameSettings(Gtk.Window):
    PAGES = [("Fan", paths.ICON),
             ("Power", ["system-shutdown"]),
             ("Timer", ["chronometer", "alarm-symbolic", "appointment-soon", "preferences-system-time"]),
             ("Reminders", ["appointment-new", "x-office-calendar", "office-calendar", "view-calendar"]),
             ("Comfort", ["weather-clear-night", "preferences-desktop-display"]),
             ("Rooms", ["go-home", "user-home", "view-grid"]),
             ("Signal", ["network-wireless-signal-excellent-symbolic", "network-wireless", "network-wireless-symbolic",
                         "network-wireless-signal-excellent"]),
             ("Recline", ["object-rotate-left", "go-bottom"]),
             ("System", ["preferences-system"])]
    TIMER_MINUTES = [1, 2, 3, 5, 10, 15, 20, 30, 45, 60]
    TIMER_LABELS = ["Timer", "Food", "Pizza", "Laundry", "Break"]
    TIMER_CHOICES = [15, 30, 45, 60, 90, 120]
    IDLE_CHOICES = [0, 5, 10, 15, 30, 60]
    SCREEN_CHOICES = [1, 2, 5, 10, 15, 30, 60]

    def __init__(self, offscreen=False):
        super().__init__(title="Deckhand")
        try:
            Gtk.Window.set_default_icon_from_file(APP_ICON)  # taskbar / window switcher icon
            self.set_icon_from_file(APP_ICON)
        except GLib.Error:
            self.set_icon_name("deckhand")
        screen = Gdk.Screen.get_default()
        self.set_default_size(screen.get_width(), screen.get_height())
        ensure_css()
        self.busy = False
        self.loading = False
        self.focused = True
        self.setup_version = None  # not known yet: asked in the background (sudo can be slow)
        self.setup_poll_until = 0.0
        self._setup_query = False
        self.jobs = SerialJobs()   # systemd changes, in order, off the UI thread
        self._live_hud = None      # renderer of the in-view readout while mapping

        self.content = add_class(Gtk.Box(), "root")
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=120)
        self.content.pack_start(self.build_rail(), False, False, 0)
        builders = {"Fan": self.build_fan, "Power": self.build_power, "Timer": self.build_timer,
                    "Reminders": self.build_reminders,
                    "Comfort": self.build_comfort, "Rooms": self.build_rooms, "Signal": self.build_signal,
                    "Recline": self.build_recline, "System": self.build_system}
        for name, _icon in self.PAGES:
            sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
            sw.add(builders[name]())
            self.stack.add_named(sw, name)
        for name, build in (("RoomPreview", self.build_room_preview_page), ("WifiMap", self.build_wifi_page),
                            ("House", self.build_house_page), ("Ceiling", self.build_ceiling_page)):
            sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
            sw.add(build())
            self.stack.add_named(sw, name)  # sub-pages of Rooms / Signal (no menu entry)
        self.content.pack_start(self.stack, True, True, 0)
        if not offscreen:
            self.add(self.content)
            self.connect("destroy", self.on_close)
        self.refresh_fan_mode()
        self.refresh_comfort()
        self.refresh_power()
        self.refresh_live()
        self.show_page("Fan")
        self.check_setup()
        if "--snapshot" not in sys.argv:
            # A readout or mini-map left in view by an earlier run that didn't close cleanly
            subprocess.run(["systemctl", "--user", "stop", "--no-block", self.WIFI_HUD_UNIT, self.MINIMAP_UNIT],
                           capture_output=True)
            in_background(updater.migrate_units, lambda _r: None)  # background services follow this copy
            if not offscreen:  # Steam's "Exit game", logout, Ctrl+C: close properly (stops readouts)
                for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
                    GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, lambda *_: (self.destroy(), False)[1])
        # Light on battery: live numbers every 3 s, the timer clock every 1 s only while its page
        # shows, and nothing at all while the window isn't focused (refreshed again on return).
        self.connect("focus-out-event", self.on_focus_change, False)
        self.connect("focus-in-event", self.on_focus_change, True)
        GLib.timeout_add_seconds(3, safe(self.refresh_live))
        GLib.timeout_add_seconds(1, safe(self.tick_timer))  # _seconds: wake-ups line up with others

    def on_close(self, *_):
        if getattr(self, "_closing", False):
            return
        self._closing = True
        if getattr(self, "wifi_rec", None):  # don't leave the in-view readout or tracking running
            self.stop_wifi_mapping()
        if getattr(self, "house_rec", None):
            self.stop_house_walk()
        if getattr(self, "house_watch", None):
            self.house_watch_stop()
        # The readouts also end by themselves once this process is gone (they watch its PID).
        subprocess.run(["systemctl", "--user", "stop", "--no-block", self.WIFI_HUD_UNIT, self.MINIMAP_UNIT],
                       capture_output=True)
        Gtk.main_quit()

    def on_focus_change(self, _w, _ev, focused):
        self.focused = focused
        if focused:
            self.refresh_live()
            self.show_page(self.stack.get_visible_child_name())
        elif getattr(self, "house_watch", None):
            self.house_watch_stop()  # nobody is looking at the house map: stop following the headset
        return False

    # ============================================================== rail
    def build_rail(self):
        rail = add_class(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10), "rail")
        rail.set_size_request(330, -1)
        head = Gtk.Box(spacing=14, margin_bottom=18)
        head.pack_start(icon_image(paths.ICON, 64), False, False, 0)
        title = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        title.pack_start(label("Deckhand", "app-title"), False, False, 0)
        title.pack_start(label(f"version {updater.local_version()}", "rail-status"), False, False, 0)
        head.pack_start(title, False, False, 0)
        rail.pack_start(head, False, False, 0)
        self.nav = {}
        for name, icon in self.PAGES:
            b = add_class(Gtk.Button(), "nav")
            inner = Gtk.Box(spacing=18)
            inner.pack_start(icon_image(icon, 44), False, False, 0)
            inner.pack_start(label(name), False, False, 0)
            b.add(inner)
            b.connect("clicked", lambda _b, n=name: self.show_page(n))
            rail.pack_start(b, False, False, 0)
            self.nav[name] = b
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.destroy())
        rail.pack_end(close, False, False, 0)
        self.rail_status = label("", "rail-status", wrap=True)
        rail.pack_end(self.rail_status, False, False, 12)
        return rail

    def show_page(self, name):
        if name != "House" and getattr(self, "house_watch", None):
            self.house_watch_stop()  # only follow the headset while the house map is on screen
        self.stack.set_visible_child_name(name)
        nav_name = {"RoomPreview": "Rooms", "Ceiling": "Rooms", "WifiMap": "Signal", "House": "Signal"}.get(name, name)
        for n, b in self.nav.items():
            ctx = b.get_style_context()
            (ctx.add_class if n == nav_name else ctx.remove_class)("active")
        {"Fan": self.refresh_live, "Power": self.refresh_power, "Comfort": self.refresh_comfort,
         "Recline": self.refresh_recline,
         "Timer": self.tick_timer, "Rooms": self.refresh_rooms,
         "System": lambda: (self.refresh_system(), self.show_restart_if_needed(),
                            None if self.upd_checked else self.on_update_check()),
         "Reminders": self.refresh_reminders, "Signal": self.refresh_signal,
         "WifiMap": lambda: self.refresh_wifi(full=True), "House": lambda: self.refresh_house(full=True),
         "Ceiling": self.refresh_ceiling_status,
         }.get(name, lambda: None)()

    def page(self, title):
        box = add_class(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22), "page")
        box.pack_start(label(title, "page-title"), False, False, 0)
        return box

    @staticmethod
    def two_columns(*cards_):
        grid = Gtk.Grid(column_spacing=22, row_spacing=22, column_homogeneous=True)
        for i, c in enumerate(cards_):
            grid.attach(c, i % 2, i // 2, 1, 1)
        return grid

    # ================================================================ fan
    def build_fan(self):
        box = add_class(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22), "page")
        header = Gtk.Box(spacing=30)
        header.pack_start(label("Fan", "page-title"), False, False, 0)
        self.rpm_lbl = label("–", "big")
        header.pack_end(self.rpm_lbl, False, False, 0)
        live = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER)
        self.temps_lbl = label("", "status", xalign=1.0, wrap=True)
        self.mode_lbl = label("", "desc", xalign=1.0, wrap=True)
        self.temps_lbl.set_justify(Gtk.Justification.RIGHT)
        self.mode_lbl.set_justify(Gtk.Justification.RIGHT)
        live.pack_start(self.temps_lbl, False, False, 0)
        live.pack_start(self.mode_lbl, False, False, 0)
        header.pack_end(live, True, True, 0)
        box.pack_start(header, False, False, 0)

        presets = card("Presets", "Tap one. The Frame has one fan; it is the air you feel on your face.")
        tiles = Gtk.Box(spacing=18, homogeneous=True)
        self.preset_btns = {}
        descs = {"Stock": "Valve's default.\nNever below 30 (40 charging).",
                 "Quiet": "No minimum.\nFollows temperature.",
                 "Extra quiet": "No minimum, 70% strength.\nLeast air, runs warmer."}
        for name in PRESETS:
            b = add_class(Gtk.Button(), "tile")
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, valign=Gtk.Align.CENTER)
            inner.pack_start(label(name, "tile-name", xalign=0.5), False, False, 0)
            inner.pack_start(label(descs[name], "tile-desc", xalign=0.5, wrap=True), False, False, 0)
            b.add(inner)
            b.connect("clicked", self.on_preset, name)
            tiles.pack_start(b, True, True, 0)
            self.preset_btns[name] = b
        presets.pack_start(tiles, False, False, 0)
        box.pack_start(presets, False, False, 0)

        custom = card("Custom", "Set your own. Lower strength = less air but a warmer headset. "
                                "Above 95 °C the fan always goes to full speed.")
        self.floor_adj = Gtk.Adjustment(value=0, lower=0, upper=FLOOR_MAX, step_increment=1, page_increment=5)
        self.strength_adj = Gtk.Adjustment(value=100, lower=50, upper=100, step_increment=5, page_increment=10)
        grid = Gtk.Grid(column_spacing=18, row_spacing=14)
        grid.attach(label("Minimum speed", "status"), 0, 0, 1, 1)
        grid.attach(Stepper(self.floor_adj, 5, {30: "stock"}), 1, 0, 1, 1)
        grid.attach(label("Strength %", "status"), 0, 1, 1, 1)
        grid.attach(Stepper(self.strength_adj, 5), 1, 1, 1, 1)
        custom.pack_start(grid, False, False, 0)
        act = Gtk.Box(spacing=18)
        self.apply_btn = add_class(Gtk.Button(label="Apply custom"), "primary")
        self.apply_btn.connect("clicked", self.on_apply_custom)
        act.pack_start(self.apply_btn, False, False, 0)
        self.fan_result = label("", "status", wrap=True)
        act.pack_start(self.fan_result, True, True, 0)
        custom.pack_start(act, False, False, 0)

        self.setup_box = card("Setup needed")
        self.setup_msg = label("", "desc", wrap=True)
        self.setup_box.pack_start(self.setup_msg, False, False, 0)
        setup_btn = add_class(Gtk.Button(label="Run setup now"), "primary")
        setup_btn.connect("clicked", self.on_run_setup)
        self.setup_box.pack_start(setup_btn, False, False, 0)
        cmd = Gtk.Entry()
        cmd.set_text(f"sudo {os.path.join(ROOT, 'setup.sh')}")
        cmd.set_editable(False)
        self.setup_box.pack_start(cmd, False, False, 0)

        row = Gtk.Box(spacing=22, homogeneous=False)
        row.pack_start(custom, True, True, 0)
        row.pack_start(self.setup_box, True, True, 0)
        box.pack_start(row, False, False, 0)
        return box

    @property
    def setup_ready(self):
        return (self.setup_version or 0) >= HELPER_MIN_VERSION

    def check_setup(self):
        """Asks (in the background) whether the fan helper is set up, then updates the Fan page."""
        if self._setup_query:
            return
        self._setup_query = True

        def done(v):
            self._setup_query = False
            was = self.setup_ready
            self.setup_version = v if isinstance(v, int) else 0
            self.show_setup(not self.setup_ready)
            if self.setup_ready and not was and self.setup_poll_until:
                self.setup_poll_until = 0.0
                set_result(self.fan_result, "Setup done. Pick a preset.", True)
        in_background(helper_version, done)

    def show_setup(self, visible):
        if self.setup_version == 0:
            msg = "Needed once before the fan can be changed. It asks for your password one time."
        else:
            msg = "Needed for the newest fan and power options. It asks for your password once more."
        self.setup_msg.set_text(msg)
        self.setup_box.set_no_show_all(not visible)
        if visible:
            self.setup_box.show_all()
        else:
            self.setup_box.hide()

    def refresh_fan_mode(self):
        mode, floor, chg, strength = fan_mode()
        active = "Stock" if mode == "stock" else \
            next((n for n, p in PRESETS.items() if p == (floor, chg, strength)), None)
        if mode == "stock":
            text = f"Now: Stock (minimum {floor}, {chg} while charging)"
        else:
            text = f"Now: {active or 'Custom'} (minimum {floor}" + \
                   (f", {chg} while charging" if chg != floor else "") + f", strength {strength}%)"
            self.floor_adj.set_value(floor)
            self.strength_adj.set_value(strength)
        self.mode_lbl.set_text(text)
        for n, b in self.preset_btns.items():
            ctx = b.get_style_context()
            (ctx.add_class if n == active else ctx.remove_class)("selected")
        if self.setup_version is not None:
            self.show_setup(not self.setup_ready)
        if mode != "stock" and fan_controller_outdated():
            set_result(self.fan_result, "SteamOS updated Valve's fan controller since this was set: tap your "
                                        "preset (or Apply custom) again to use the new one.", False)

    def refresh_live(self):
        if not self.focused and time.time() >= self.setup_poll_until:
            return True  # in the background: do nothing until focused again
        page = self.stack.get_visible_child_name()
        s = fan_status(temps=page == "Fan")  # temperatures only show on the Fan page
        self.rpm_lbl.set_text(f"{s['rpm']:,} rpm" if s["rpm"] is not None else "fan: n/a")
        parts = []
        if s["cpu"] is not None:
            parts.append(f"CPU {s['cpu']:.0f} °C")
        if s["gpu"] is not None:
            parts.append(f"GPU {s['gpu']:.0f} °C")
        bat = (f"Battery {s['battery']}%" if s["battery"] is not None else "Battery n/a") + \
            (" (charging)" if s["charging"] else "")
        parts.append(bat)
        if page == "Fan":
            self.temps_lbl.set_text("   ·   ".join(parts))
        self.rail_status.set_text(bat + (f"\nFan {s['rpm']:,} rpm" if s["rpm"] is not None else ""))
        # live signal readings, only while their page is open
        if page == "WifiMap" and not (self.wifi_rec and self.wifi_rec.running):
            self.refresh_wifi()
        elif page == "House" and not (self.house_rec and self.house_rec.running):
            self.refresh_house()
        elif page == "Signal":
            self.refresh_signal()
        if self.stack.get_visible_child_name() == "Power":
            self.refresh_power_status()
        if not self.setup_ready and time.time() < self.setup_poll_until:
            self.check_setup()  # waiting for the setup terminal to finish
        return True

    def apply_fan(self, args, what):
        if self.busy:
            return
        if self.setup_version is not None and not self.setup_ready:
            set_result(self.fan_result, "Run the setup first (on the right).", False)
            self.show_setup(True)
            return
        self.busy = True
        self.apply_btn.set_sensitive(False)
        set_result(self.fan_result, f"Applying {what}…", True)

        def done(result):
            self.busy = False
            self.apply_btn.set_sensitive(True)
            ok, out = result if isinstance(result, tuple) else (False, str(result))
            if not ok and "setup" in out:
                self.setup_version = 0
            set_result(self.fan_result, f"{what}: done" if ok else f"Failed: {out}", ok)
            self.refresh_fan_mode()
        in_background(lambda: run_helper(args), done)

    def on_preset(self, _btn, name):
        p = PRESETS[name]
        self.apply_fan(["off"] if p is None else ["on", *map(str, p)], name)

    def on_apply_custom(self, *_):
        floor = int(self.floor_adj.get_value())
        strength = int(self.strength_adj.get_value())
        self.apply_fan(["on", str(floor), str(floor), str(strength)], "Custom")

    def on_run_setup(self, *_):
        """Opens a terminal running the setup, so the password is typed there (never here)."""
        setup = os.path.join(ROOT, "setup.sh")
        for term in (["konsole", "--hold", "-e"], ["xterm", "-hold", "-e"]):
            try:
                subprocess.Popen([*term, "sudo", setup], start_new_session=True)
                self.setup_poll_until = time.time() + 300
                set_result(self.fan_result, "Type your password in the terminal that opened, then close it.", True)
                return
            except OSError:
                continue
        set_result(self.fan_result, "No terminal found; run the command shown yourself.", False)

    # ============================================================== power
    def build_power(self):
        box = self.page("Power")
        box.pack_start(label("Taking the Frame off or tapping the power button only puts it in standby "
                             "(screens off). It stays on and drains the battery. Shut it down here, "
                             "or let it shut itself down.", "desc", wrap=True), False, False, 0)
        box.pack_start(ConfirmButton("Shut down now", "Tap again to shut down", self.do_shutdown),
                       False, False, 0)

        timer = card("Sleep timer", "Tap a time to start. Shuts down when it runs out "
                                    "(with a warning one minute before).")
        self.timer_seg = Segmented([(m, f"{m}") for m in self.TIMER_CHOICES], self.on_timer_pick)
        timer.pack_start(self.timer_seg, False, False, 0)
        timer.pack_start(label("minutes", "desc", xalign=0.5), False, False, 0)
        self.timer_lbl = label("", "status", wrap=True)
        timer.pack_start(self.timer_lbl, False, False, 0)
        cancel = Gtk.Button(label="Cancel timer")
        cancel.connect("clicked", self.on_timer_cancel)
        timer.pack_start(cancel, False, False, 0)

        auto = card("Auto shut-off", "Shuts down after the headset has been in standby (off your head) "
                                     "this long. Putting it back on restarts the count.")
        self.idle_seg = Segmented([(m, "Off" if m == 0 else f"{m}") for m in self.IDLE_CHOICES],
                                  self.on_idle_pick)
        auto.pack_start(self.idle_seg, False, False, 0)
        auto.pack_start(label("minutes in standby", "desc", xalign=0.5), False, False, 0)
        self.charge_switch = Gtk.Switch()
        self.charge_switch.connect("notify::active", self.on_charge_toggled)
        auto.pack_start(switch_row("Not while charging", label("Plugged in? Then stay on.", "desc"),
                                   self.charge_switch), False, False, 0)
        self.power_status_lbl = label("", "status", wrap=True)
        auto.pack_start(self.power_status_lbl, False, False, 0)

        box.pack_start(self.two_columns(timer, auto), False, False, 0)
        self.power_result = label("", "status", wrap=True)
        box.pack_start(self.power_result, False, False, 0)
        return box

    def refresh_power(self):
        cfg = power.load_config()
        self.loading = True
        self.idle_seg.set_value(int(cfg.get("idle_off_minutes") or 0))
        self.charge_switch.set_active(bool(cfg.get("skip_while_charging", True)))
        self.loading = False
        self.refresh_power_status()

    def refresh_power_status(self):
        cfg = power.load_config()
        deadline = cfg.get("timer_deadline")
        if deadline and deadline > time.time():
            left = int((deadline - time.time()) / 60) + 1
            self.timer_lbl.set_text(f"Shuts down at {time.strftime('%H:%M', time.localtime(deadline))} "
                                    f"(in about {left} min)")
        else:
            self.timer_lbl.set_text("No timer running")
            self.timer_seg.set_value(None)
        st = power.read_status()
        now = time.time()
        if now - getattr(self, "_svc_checked", 0) > 15 and not getattr(self, "_svc_query", False):
            self._svc_query = True  # asking systemd launches a process: in the background, not every tick

            def got(active):
                self._svc_query = False
                self._svc_active = active is True
                self._svc_checked = time.time()
                if self.stack.get_visible_child_name() == "Power":
                    self.refresh_power_status()
            in_background(power.service_active, got)
        running = getattr(self, "_svc_active", True)
        idle = int(cfg.get("idle_off_minutes") or 0) if isinstance(cfg.get("idle_off_minutes"), (int, float)) else 0
        if not running:
            lines = ["Auto shut-off: service not running"]
        elif idle == 0:
            lines = ["Auto shut-off: off (the sleep timer still works)"]
        else:
            lines = [f"Auto shut-off: on, after {idle} min in standby"]
        if running and st:
            if st.get("standby_since"):
                lines.append(f"Headset in standby for {int((time.time() - st['standby_since']) / 60)} min")
            elif st.get("steamvr"):
                lines.append("Headset in use")
            if st.get("held"):
                lines.append(f"Shut-off is waiting: {st['held']}")
            elif isinstance(st.get("off_at"), (int, float)) and st["off_at"] > time.time():
                lines.append(f"Shuts down at {time.strftime('%H:%M', time.localtime(st['off_at']))} "
                             "if it stays in standby")
        self.power_status_lbl.set_text("\n".join(lines))

    def power_change(self, mutate, message):
        cfg = power.load_config()
        mutate(cfg)
        power.save_config(cfg)

        def done(result):
            ok, out = result if isinstance(result, tuple) else (False, str(result))
            set_result(self.power_result, message if ok else f"Saved, but {out}", ok)
            self._svc_checked = 0  # ask systemd again: it may have just started
            self.refresh_power_status()
        in_background(power.ensure_service, done)

    def do_shutdown(self):
        set_result(self.power_result, "Shutting down…", True)
        def done(r):
            ok = r is True or (isinstance(r, tuple) and r and r[0] is True)
            if not ok:
                set_result(self.power_result, "Shutdown failed: see logs/power.log", False)
        in_background(lambda: power.shutdown("Shut down now (Deckhand)"), done)

    def on_timer_pick(self, m):
        deadline = time.time() + m * 60
        self.power_change(lambda c: c.update(timer_deadline=deadline),
                          f"Sleep timer: shuts down at {time.strftime('%H:%M', time.localtime(deadline))}")

    def on_timer_cancel(self, *_):
        self.timer_seg.set_value(None)
        self.power_change(lambda c: c.update(timer_deadline=None), "Sleep timer cancelled")

    def on_idle_pick(self, m):
        self.power_change(lambda c: c.update(idle_off_minutes=m),
                          "Auto shut-off off" if m == 0 else f"Auto shut-off after {m} min in standby")

    def on_charge_toggled(self, sw, _pspec):
        if not self.loading:
            on = sw.get_active()
            self.power_change(lambda c: c.update(skip_while_charging=on),
                              "Stays on while charging" if on else "Auto shut-off also while charging")

    # ============================================================ comfort
    def build_comfort(self):
        box = self.page("Comfort")
        dnd = card()
        self.dnd_switch = Gtk.Switch()
        self.dnd_switch.connect("notify::active", self.on_bool_setting, "notifications", "DoNotDisturb")
        dnd.pack_start(switch_row("Do Not Disturb", label("Hide SteamVR notifications. Handy when watching "
                                                          "or falling asleep.", "desc", wrap=True),
                                  self.dnd_switch), False, False, 0)

        shots = card()
        # Switch ON = screenshots blocked (SteamVR's userinterface.screenshots = false).
        self.shot_switch = Gtk.Switch()
        self.shot_sub = label("", "desc", wrap=True)
        self.shot_switch.connect("notify::active", self.on_disable_shots)
        self.shot_switch.connect("notify::active", lambda *_: self.update_shot_text())
        shots.pack_start(switch_row("Disable screenshots", self.shot_sub, self.shot_switch), False, False, 0)
        shots.pack_start(label("How: hold the Steam button and pull a trigger. They appear in Steam → Media.",
                               "desc", wrap=True), False, False, 0)
        shots.set_tooltip_markup(SHOT_TOOLTIP)

        screens = card("Screens off when idle", "How long the headset sits idle before the displays "
                                                "turn off. Default is 5.")
        self.screens_seg = Segmented([(m, f"{m}") for m in self.SCREEN_CHOICES], self.on_screens_pick)
        screens.pack_start(self.screens_seg, False, False, 0)
        screens.pack_start(label("minutes", "desc", xalign=0.5), False, False, 0)

        theater = card("Theater surroundings", "Brightness of the room around the big screen when "
                                               "watching video. Lower is darker, easier at night. Default 50.")
        self.theater_adj = Gtk.Adjustment(value=50, lower=0, upper=100, step_increment=5, page_increment=10)
        theater.pack_start(Stepper(self.theater_adj, 10, {50: "default"}, on_change=self.on_theater),
                           False, False, 0)

        box.pack_start(self.two_columns(dnd, shots, screens, theater), False, False, 0)
        self.comfort_result = label("", "status")
        box.pack_start(self.comfort_result, False, False, 0)
        box.pack_start(label("These are SteamVR settings and take effect right away.", "desc"), False, False, 0)
        return box

    def update_shot_text(self):
        if self.shot_switch.get_active():
            self.shot_sub.set_text("ON: screenshots are blocked. Steam button + trigger won't take one, "
                                   "even by accident.")
        else:
            self.shot_sub.set_text("OFF (default): screenshots work.")

    def on_disable_shots(self, switch, _pspec):
        if not self.loading:
            allow = not switch.get_active()
            self.comfort_change("Screenshots allowed" if allow else "Screenshots disabled",
                                "userinterface", "screenshots", "bool", allow)

    def refresh_comfort(self):
        self.loading = True
        self.dnd_switch.set_active(vr_get("notifications", "DoNotDisturb", "bool", False))
        self.shot_switch.set_active(not vr_get("userinterface", "screenshots", "bool", True))
        self.update_shot_text()
        self.screens_seg.set_value(int(round(vr_get("power", "turnOffScreensTimeout", "float", 5.0))))
        self.theater_adj.set_value(round(vr_get("dashboard", "theaterModeBrightness", "float", 0.5) * 100))
        self.loading = False

    def comfort_change(self, desc, section, key, kind, value):
        def done(result):
            ok = result is True
            set_result(self.comfort_result, f"{desc}: saved" if ok else f"{desc}: SteamVR did not take it", ok)
            if not ok:
                self.refresh_comfort()
        in_background(lambda: vr_set(section, key, kind, value), done)

    def on_bool_setting(self, switch, _pspec, section, key):
        if not self.loading:
            on = switch.get_active()
            self.comfort_change(f"Do Not Disturb {'on' if on else 'off'}", section, key, "bool", on)

    def on_screens_pick(self, m):
        self.comfort_change(f"Screens off after {m} min", "power", "turnOffScreensTimeout", "float", float(m))

    def on_theater(self):
        if not self.loading:
            v = int(self.theater_adj.get_value())
            self.comfort_change(f"Theater surroundings {v}%", "dashboard", "theaterModeBrightness", "float", v / 100)

    # ============================================================== timer
    def build_timer(self):
        box = self.page("Timer")
        now_card = card()
        row = Gtk.Box(spacing=30)
        self.timer_big = label("No timer", "big")
        row.pack_start(self.timer_big, False, False, 0)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, valign=Gtk.Align.CENTER)
        self.timer_status = label("", "status", wrap=True)
        right.pack_start(self.timer_status, False, False, 0)
        btns = Gtk.Box(spacing=14)
        cancel = Gtk.Button(label="Cancel timer")
        cancel.connect("clicked", self.on_kitchen_cancel)
        btns.pack_start(cancel, False, False, 0)
        preview = Gtk.Button(label="Preview popup (10 s)")
        preview.connect("clicked", lambda *_: self.start_kitchen_timer(10, preview=True))
        self._replace_timer_until = 0.0
        btns.pack_start(preview, False, False, 0)
        right.pack_start(btns, False, False, 0)
        row.pack_start(right, True, True, 0)
        now_card.pack_start(row, False, False, 0)
        box.pack_start(now_card, False, False, 0)

        start = card("Start a timer", "Tap a time to start. For the last minute a countdown pops up at the "
                                      "top of your view; at zero it rings. Works with this app closed.")
        start.pack_start(label("What for?", "status"), False, False, 0)
        self.timer_label_seg = Segmented([(t, t) for t in self.TIMER_LABELS],
                                         lambda _v: self.timer_name.set_text(""))
        self.timer_label_seg.set_value("Timer")
        start.pack_start(self.timer_label_seg, False, False, 0)
        self.timer_name = Gtk.Entry(placeholder_text="…or type your own name", max_length=40)
        self.timer_name.connect("changed", lambda e: self.timer_label_seg.set_value(
            None if e.get_text().strip() else (self.timer_label_seg.value or "Timer")))
        start.pack_start(self.timer_name, False, False, 0)
        start.pack_start(label("How long? (minutes)", "status"), False, False, 0)
        half = len(self.TIMER_MINUTES) // 2
        for chunk in (self.TIMER_MINUTES[:half], self.TIMER_MINUTES[half:]):
            seg = Segmented([(m, str(m)) for m in chunk], lambda m: self.start_kitchen_timer(m * 60))
            start.pack_start(seg, False, False, 0)
        custom = Gtk.Box(spacing=14)
        custom.pack_start(label("Other:", "status"), False, False, 0)
        self.kitchen_adj = Gtk.Adjustment(value=25, lower=1, upper=180, step_increment=1, page_increment=5)
        custom.pack_start(Stepper(self.kitchen_adj, 1), True, True, 0)
        go = add_class(Gtk.Button(label="Start"), "primary")
        go.connect("clicked", lambda *_: self.start_kitchen_timer(int(self.kitchen_adj.get_value()) * 60))
        custom.pack_start(go, False, False, 0)
        start.pack_start(custom, False, False, 0)
        box.pack_start(start, False, False, 0)
        self.timer_result = label("", "status", wrap=True)
        box.pack_start(self.timer_result, False, False, 0)
        return box

    def render_timer_images(self, text):
        """Countdown pictures for the headset popup: 00..60.rgba and done.rgba. They only change
        with the timer's name (and the app's look), so they are drawn again only then."""
        out = kitchen_timer.IMG_DIR
        stamp = hashlib.sha1((text + "\0" + CSS.decode() + "\0" + updater.local_version()).encode()).hexdigest()
        stamp_file = os.path.join(out, "stamp.txt")
        files = [f"{s:02d}.rgba" for s in range(61)] + ["done.rgba"]
        if read(stamp_file) == stamp and all(os.path.exists(os.path.join(out, f)) for f in files):
            return
        os.makedirs(out, exist_ok=True)
        for f in os.listdir(out):
            if f.endswith((".png", ".rgba", ".txt")):
                os.remove(os.path.join(out, f))
        with HudRenderer() as r:
            for sec in range(61):
                r.render(text, f"{sec // 60}:{sec % 60:02d}", os.path.join(out, f"{sec:02d}.rgba"))
            r.render(text, "Time's up!", os.path.join(out, "done.rgba"), done=True)
        with open(stamp_file, "w", encoding="utf-8") as f:
            f.write(stamp)

    def start_kitchen_timer(self, seconds, preview=False):
        if getattr(self, "_timer_busy", False):
            return  # a double tap: the first one is still starting
        running = kitchen_timer.status()
        if running and running["end"] > time.time():
            if preview:
                set_result(self.timer_result, "A timer is running: the preview would replace it. "
                                              "Cancel the timer first.", False)
                return
            if time.monotonic() > self._replace_timer_until:  # one mis-aimed tap mustn't replace it
                self._replace_timer_until = time.monotonic() + 5
                set_result(self.timer_result, f"{running['label']} is still running: tap again within 5 s "
                                              "to replace it.", False)
                return
        self._replace_timer_until = 0.0
        what = self.timer_name.get_text().strip() or self.timer_label_seg.value or "Timer"
        set_info(self.timer_result, "Getting the popup ready…")
        try:
            self.render_timer_images(what)
        except Exception as e:  # noqa: BLE001
            set_result(self.timer_result, f"Couldn't draw the popup: {e}", False)
            return
        self._timer_busy = True

        def done(result):
            self._timer_busy = False
            ok, msg = result if isinstance(result, tuple) else (False, str(result))
            if ok:
                end = time.strftime("%H:%M", time.localtime(time.time() + seconds))
                set_result(self.timer_result, "Preview: the popup shows in your view now and rings at zero."
                           if preview else f"{what} timer set for {seconds // 60} min (rings at {end}).", True)
            else:
                set_result(self.timer_result, f"Couldn't start the timer: {msg}", False)
            self.tick_timer()
        # stopping a running timer waits for its popup to close (about a second): not on the UI thread
        in_background(lambda: kitchen_timer.start(seconds, what), done)

    def on_kitchen_cancel(self, *_):
        set_info(self.timer_result, "Cancelling…")

        def done(_r):
            set_result(self.timer_result, "Timer cancelled.", True)
            self.tick_timer()
        in_background(kitchen_timer.cancel, done)

    def tick_timer(self):
        if not self.focused or self.stack.get_visible_child_name() != "Timer":
            return True  # only the Timer page shows the clock
        st = kitchen_timer.status()
        if st:
            left = max(0, int(st["end"] - time.time() + 0.999))
            self.timer_big.set_text(f"{left // 60}:{left % 60:02d}")
            when = time.strftime("%H:%M", time.localtime(st["end"]))
            self.timer_status.set_text(f"{st['label']}: rings at {when}. The countdown pops up in your view "
                                       f"for the last minute." if left > 0 else f"{st['label']}: time's up!")
        else:
            self.timer_big.set_text("No timer")
            self.timer_status.set_text("Pick a time below to start one.")
        return True

    # ========================================================== reminders
    REMINDER_LABELS = ["Reminder", "Food", "Pizza", "Laundry", "Meds", "Call"]

    def build_reminders(self):
        box = self.page("Reminders")
        box.pack_start(label("Pops up at the top of your view at that time and rings. Works with this app "
                             "closed; if the Frame is off then, it shows when it next starts.", "desc", wrap=True),
                       False, False, 0)
        when_card = card("When")
        today = datetime.date.today()
        names = ["Today", "Tomorrow"] + [(today + datetime.timedelta(days=d)).strftime("%a %d")
                                         for d in range(2, 7)]
        self.rem_day_seg = Segmented(list(enumerate(names)), lambda d: self.rem_day.set(d, notify=True),
                                     per_row=4)
        self._rem_date = today
        when_card.pack_start(self.rem_day_seg, False, False, 0)
        self.rem_day = Spin(0, 365, 1, 0, wrap=False, width_chars=11,
                            fmt=lambda d: (datetime.date.today() + datetime.timedelta(days=d)).strftime("%a, %b %d"),
                            on_change=self.on_rem_day)
        when_card.pack_start(self.rem_day, False, False, 0)

        start = datetime.datetime.now() + datetime.timedelta(minutes=20)
        start = start.replace(minute=(start.minute // 5) * 5)
        trow = Gtk.Box(spacing=12)
        self.rem_hour = Spin(1, 12, 1, start.hour % 12 or 12, width_chars=2,
                             on_change=lambda _v: self.update_rem_summary())
        self.rem_min = Spin(0, 55, 5, start.minute, fmt=lambda m: f"{m:02d}", width_chars=2,
                            on_change=lambda _v: self.update_rem_summary())
        trow.pack_start(self.rem_hour, True, True, 0)
        trow.pack_start(label(":", "spin-value"), False, False, 0)
        trow.pack_start(self.rem_min, True, True, 0)
        when_card.pack_start(trow, False, False, 0)
        self.rem_ampm = Segmented([("AM", "AM"), ("PM", "PM")], lambda _v: self.update_rem_summary())
        self.rem_ampm.set_value("PM" if start.hour >= 12 else "AM")
        when_card.pack_start(self.rem_ampm, False, False, 0)

        what = card("What")
        self.rem_label_seg = Segmented([(t, t) for t in self.REMINDER_LABELS],
                                       lambda _v: (self.rem_entry.set_text(""), self.update_rem_summary()),
                                       per_row=3)
        self.rem_label_seg.set_value("Reminder")
        what.pack_start(self.rem_label_seg, False, False, 0)
        self.rem_entry = Gtk.Entry(placeholder_text="…or type your own", max_length=80)
        self.rem_entry.connect("changed", lambda *_: self.update_rem_summary())
        what.pack_start(self.rem_entry, False, False, 0)
        self.rem_summary = label("", "card-title", wrap=True)
        what.pack_start(self.rem_summary, False, False, 0)
        set_btn = add_class(Gtk.Button(label="Set reminder"), "primary")
        set_btn.connect("clicked", self.on_set_reminder)
        what.pack_start(set_btn, False, False, 0)
        self.rem_set_btn = set_btn
        self.rem_result = label("", "status", wrap=True)
        what.pack_start(self.rem_result, False, False, 0)
        what.pack_start(label("Upcoming", "card-title"), False, False, 0)
        self.rem_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        what.pack_start(self.rem_list, False, False, 0)

        box.pack_start(self.two_columns(when_card, what), False, False, 0)
        self.rem_day_seg.set_value(0)
        self.update_rem_summary()
        return box

    def on_rem_day(self, d):
        self.rem_day_seg.set_value(d if d < 7 else None)
        self.update_rem_summary()

    def rem_when(self):
        h = self.rem_hour.value % 12 + (12 if self.rem_ampm.value == "PM" else 0)
        day = datetime.date.today() + datetime.timedelta(days=self.rem_day.value)
        return datetime.datetime.combine(day, datetime.time(h, self.rem_min.value))

    def rem_text(self):
        typed = " ".join(self.rem_entry.get_text().split())  # pasted newlines/tabs become spaces
        return typed or self.rem_label_seg.value or "Reminder"

    def rem_new_day(self):
        """Left open past midnight: "Tomorrow", "Fri 28" and the date shown must move on too."""
        today = datetime.date.today()
        if today == getattr(self, "_rem_date", today):
            return
        self._rem_date = today
        names = ["Today", "Tomorrow"] + [(today + datetime.timedelta(days=d)).strftime("%a %d") for d in range(2, 7)]
        for b, n in zip(self.rem_day_seg.buttons, names):
            b.set_label(n)
        self.rem_day.set(self.rem_day.value)

    def update_rem_summary(self):
        when = self.rem_when()
        delta = when - datetime.datetime.now()
        if delta.total_seconds() <= 0:
            rel = "that time has passed"
        else:
            mins = int(delta.total_seconds() // 60)
            rel = (f"in {mins // 1440} d {mins % 1440 // 60} h" if mins >= 1440 else
                   f"in {mins // 60} h {mins % 60} min" if mins >= 60 else f"in {mins} min")
        self.rem_summary.set_text(f"{self.rem_text()}: {when.strftime('%a, %b %d at %I:%M %p').replace(' 0', ' ')}"
                                  f"  ({rel})")

    def on_set_reminder(self, *_):
        self.rem_new_day()
        when = self.rem_when()
        epoch = when.timestamp()
        text = self.rem_text()
        if epoch <= time.time():
            set_result(self.rem_result, "That time has already passed; pick a later one.", False)
            return
        dup = next((m for m in reminders.upcoming() if abs(m["when"] - epoch) < 30 and m["text"] == text), None)
        if dup:
            set_result(self.rem_result, "That reminder is already set.", False)
            return
        rid = reminders.new_id(epoch)
        try:
            with HudRenderer() as r:
                r.render(f"Reminder · {when.strftime('%I:%M %p').lstrip('0')}", text,
                         reminders.image_path(rid), done=True)
        except Exception as e:  # noqa: BLE001
            set_result(self.rem_result, f"Couldn't draw the popup: {e}", False)
            return
        self.rem_set_btn.set_sensitive(False)  # one tap, one reminder

        def done(result):
            self.rem_set_btn.set_sensitive(True)
            ok, msg = result if isinstance(result, tuple) else (False, str(result))
            set_result(self.rem_result, f"Set: {text}, {when.strftime('%a %b %d, %I:%M %p')}." if ok
                       else f"Couldn't set it: {msg}", ok)
            self.refresh_reminders()
        in_background(lambda: reminders.add(rid, epoch, text), done)  # systemctl calls: off the UI thread

    def refresh_reminders(self):
        self.rem_new_day()
        for child in self.rem_list.get_children():
            self.rem_list.remove(child)
        items = reminders.upcoming()
        if not items:
            self.rem_list.pack_start(label("None yet.", "desc"), False, False, 0)
        for m in items:
            row = Gtk.Box(spacing=12)
            when = datetime.datetime.fromtimestamp(m["when"])
            txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            txt.pack_start(label(m["text"], "status", wrap=True), False, False, 0)
            txt.pack_start(label(when.strftime("%a, %b %d at %I:%M %p"), "desc"), False, False, 0)
            row.pack_start(txt, True, True, 0)
            row.pack_end(ConfirmButton("Delete", "Tap again", lambda rid=m["id"]: self.delete_reminder(rid),
                                       css="primary"), False, False, 0)
            self.rem_list.pack_start(row, False, False, 0)
        self.rem_list.show_all()
        self.update_rem_summary()

    def delete_reminder(self, rid):
        def done(_r):
            set_result(self.rem_result, "Reminder deleted.", True)
            self.refresh_reminders()
        in_background(lambda: reminders.delete(rid), done)

    # ============================================================== rooms
    def build_rooms(self):
        box = self.page("Rooms")
        box.pack_start(label("Save each room you play in, then load it to get its boundary back without "
                             "redrawing. Loading restarts SteamVR (about 10 s; open apps close, reopen this one "
                             "after).", "desc", wrap=True), False, False, 0)
        tools = Gtk.Box(spacing=14, homogeneous=True)
        ceil_btn = Gtk.Button(label="Ceiling indicator ›")
        ceil_btn.connect("clicked", lambda *_: self.show_ceiling_page())
        tools.pack_start(ceil_btn, True, True, 0)
        box.pack_start(tools, False, False, 0)

        cur = card("Room in use now")
        crow = Gtk.Box(spacing=18)
        self.room_now_img = Gtk.Image()
        crow.pack_start(self.room_now_img, False, False, 0)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, valign=Gtk.Align.CENTER)
        self.room_now_lbl = label("", "status", wrap=True)
        right.pack_start(self.room_now_lbl, False, False, 0)
        now_prev = Gtk.Button(label="Preview")
        now_prev.connect("clicked", lambda *_: in_background(
            rooms.current, lambda m: self.show_room_preview(m if isinstance(m, dict) else {}, current=True)))
        right.pack_start(now_prev, False, False, 0)
        self.undo_btn = ConfirmButton("Undo last load", "Tap again to switch back",
                                      lambda: self.load_room(rooms.BACKUP, "the room before the last load"),
                                      css="primary")
        right.pack_start(self.undo_btn, False, False, 0)
        crow.pack_start(right, True, True, 0)
        cur.pack_start(crow, False, False, 0)

        save = card("Save this room")
        self.room_name = Gtk.Entry(placeholder_text="Name it, e.g. Bedroom", max_length=40)
        self.room_name.connect("changed", lambda *_: self.reset_save_button())
        save.pack_start(self.room_name, False, False, 0)
        save.pack_start(Segmented([(n, n) for n in rooms.SUGGESTED_NAMES],
                                  lambda n: self.room_name.set_text(n), per_row=3), False, False, 0)
        self.room_save_btn = add_class(Gtk.Button(), "primary")
        self.room_save_lbl = label("Save", wrap=True, xalign=0.5)  # "Tap again to replace '…'" wraps
        self.room_save_lbl.set_justify(Gtk.Justification.CENTER)
        self.room_save_btn.add(self.room_save_lbl)
        self.room_save_btn.connect("clicked", self.on_save_room)
        save.pack_start(self.room_save_btn, False, False, 0)
        box.pack_start(self.two_columns(cur, save), False, False, 0)

        self.rooms_result = label("", "status", wrap=True)
        box.pack_start(self.rooms_result, False, False, 0)
        box.pack_start(label("Saved rooms", "card-title"), False, False, 0)
        self.rooms_grid_holder = Gtk.Box()
        box.pack_start(self.rooms_grid_holder, False, False, 0)
        self._replace_armed = (None, 0)
        self._replace_timer = None
        self._rooms_seq = 0
        return box

    @staticmethod
    def room_preview(m, w=300, h=200, grid=False):
        """Top-down drawing of a room's boundary (SVG → image): outline, where you stand (dot), which way
        you face (line). grid=True adds a 1 m grid, for the big preview."""
        pts = m.get("outline") or []
        bg = "#141b24"
        k = min(max(1.0, h / 200), 1.6)  # bigger drawing: somewhat thicker lines and bigger text (capped)
        if len(pts) < 3:
            svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">'
                   f'<rect width="{w}" height="{h}" rx="14" fill="{bg}"/>'
                   f'<text x="{w / 2}" y="{h / 2}" fill="#9fb3c8" font-size="{20 * k:.0f}" text-anchor="middle" '
                   f'font-family="sans-serif">No boundary drawn</text></svg>')
        else:
            xs, zs = [p[0] for p in pts], [p[1] for p in pts]
            st = m.get("standing")
            if st:
                xs.append(st[0])
                zs.append(st[1])
            minx, maxx, minz, maxz = min(xs), max(xs), min(zs), max(zs)
            pad, foot = 18 * k, 26 * k
            s = min((w - 2 * pad) / max(maxx - minx, 0.5), (h - 2 * pad - foot) / max(maxz - minz, 0.5))
            ox = (w - (maxx - minx) * s) / 2
            oz = (h - foot - (maxz - minz) * s) / 2

            def P(x, z):
                return ox + (x - minx) * s, oz + (z - minz) * s
            lines = ""
            if grid:  # 1 m squares
                gx = math.floor(minx)
                while gx <= maxx + 1e-6:
                    x1, _ = P(gx, minz)
                    lines += (f'<line x1="{x1:.1f}" y1="{oz:.1f}" x2="{x1:.1f}" '
                              f'y2="{oz + (maxz - minz) * s:.1f}" stroke="#2c3b4b" stroke-width="1.5"/>')
                    gx += 1
                gz = math.floor(minz)
                while gz <= maxz + 1e-6:
                    _, z1 = P(minx, gz)
                    lines += (f'<line x1="{ox:.1f}" y1="{z1:.1f}" x2="{ox + (maxx - minx) * s:.1f}" '
                              f'y2="{z1:.1f}" stroke="#2c3b4b" stroke-width="1.5"/>')
                    gz += 1
            poly = " ".join(f"{px:.1f},{pz:.1f}" for px, pz in (P(x, z) for x, z in pts))
            extra = ""
            if st:
                sx, sz = P(st[0], st[1])
                fx, fz = -math.sin(st[2]), -math.cos(st[2])  # facing -Z, turned by yaw
                extra = (f'<line x1="{sx:.1f}" y1="{sz:.1f}" x2="{sx + fx * 26 * k:.1f}" y2="{sz + fz * 26 * k:.1f}" '
                         f'stroke="#ffffff" stroke-width="{3 * k:.1f}" stroke-linecap="round"/>'
                         f'<circle cx="{sx:.1f}" cy="{sz:.1f}" r="{6 * k:.1f}" fill="#ffffff"/>')
            dims = f"about {max(xs[:len(pts)]) - min(xs[:len(pts)]):.1f} × " \
                   f"{max(zs[:len(pts)]) - min(zs[:len(pts)]):.1f} m · {m.get('area', 0)} m²"
            if grid:
                dims += " · grid squares are 1 m"
            svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">'
                   f'<rect width="{w}" height="{h}" rx="14" fill="{bg}"/>{lines}'
                   f'<polygon points="{poly}" fill="{ACCENT}" fill-opacity="0.22" stroke="{ACCENT}" '
                   f'stroke-width="{3 * k:.1f}" stroke-linejoin="round"/>{extra}'
                   f'<text x="{w / 2}" y="{h - 8 * k:.0f}" fill="#c9d6e3" font-size="{17 * k:.0f}" '
                   f'text-anchor="middle" font-family="sans-serif">{dims}</text></svg>')
        loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
        loader.write(svg.encode())
        loader.close()
        return loader.get_pixbuf()

    # ============================================================= signal
    def build_signal(self):
        box = self.page("Signal")
        box.pack_start(label(
            "The Frame has two separate wireless links. Home Wi-Fi is its connection to your router "
            "(internet, the store, PC streaming without the dongle). The Steam dongle is a private 6 GHz "
            "link straight from the dongle on your PC (PC VR streaming with the dongle). Map either one.",
            "desc", wrap=True), False, False, 0)
        self.sig_now = {}
        self._sig_reading, self._sig_done = False, None
        cards_ = []
        for target, title in wifi_map.TARGETS.items():
            c = card(title)
            big = label("", "big", markup=True)
            c.pack_start(big, False, False, 0)
            desc = label("", "status", wrap=True)
            c.pack_start(desc, False, False, 0)
            self.sig_now[target] = (big, desc)
            cards_.append(c)
        room = card("Play-space map", "Walk around your boundary for a minute or two: shows the best spot "
                                      "to stand for wireless play.")
        b = add_class(Gtk.Button(label="Open play-space map ›"), "primary")
        b.connect("clicked", lambda *_: self.show_wifi_page())
        room.pack_start(b, False, False, 0)
        house = card("Whole-house map", "Walk every room (several walks add up): a floor plan coloured by "
                                        "signal, with labels and a mini-map in view.")
        b = add_class(Gtk.Button(label="Open whole-house map ›"), "primary")
        b.connect("clicked", lambda *_: self.show_house_page())
        house.pack_start(b, False, False, 0)
        box.pack_start(self.two_columns(*cards_, room, house), False, False, 0)
        return box

    def refresh_signal(self):
        self.read_signals(self.show_signal_now)

    def read_signals(self, done):
        """Reads both links off the UI thread (a few `iw` calls), then done(wifi, dongle) on it. One
        read at a time; the page that asked last gets the result."""
        self._sig_done = done
        if self._sig_reading:
            return
        self._sig_reading = True

        def finish(result):
            self._sig_reading = False
            wifi, dongle = result if isinstance(result, tuple) else (None, None)
            self._sig_done(wifi, dongle)
        in_background(lambda: (wifi_map.signal(), wifi_map.dongle_signal()), finish)

    def show_signal_now(self, wifi, dongle):
        for target, s in (("wifi", wifi), ("dongle", dongle)):
            big, desc = self.sig_now[target]
            if s and s.get("dbm") is not None:
                word, colour = wifi_map.quality(s["dbm"])
                big.set_markup(f'<span foreground="{colour}">{s["dbm"]:.0f} dBm</span>')
                where = f"{s['ssid']} ({wifi_map.band(s['freq'])})" if target == "wifi" else "6 GHz, PC dongle"
                desc.set_text(f"{word.capitalize()} · {s.get('tx') or 0:.0f} Mbit/s · {where}")
            else:
                big.set_markup('<span foreground="#8a96a3">Not connected</span>')
                desc.set_text("The Frame isn't on a Wi-Fi network." if target == "wifi" else
                              "The PC's dongle isn't linked right now (PC off or asleep, or Steam closed).")

    # ========================================================== wi-fi map
    # In-view readouts while mapping: pictures rewritten every second, kept in RAM (no flash wear).
    WIFI_HUD = os.path.join(paths.runtime_dir(), "wifi-live.rgba")
    WIFI_HUD_UNIT = "frame-wifi-live"
    MINIMAP = os.path.join(paths.runtime_dir(), "minimap.rgba")
    MINIMAP_UNIT = "frame-minimap"

    def set_readout(self, mode=None):
        """Which in-view readout runs: None, "--live" (big number) or "--minimap". Each is its own user
        service that ends with SteamVR (PartOf) and by itself once this window is gone (it watches our
        PID). Stopping an overlay takes a second or two, so changes run in order in the background."""
        self._readout_want = mode
        self.jobs.put(self._apply_readout, self._readout_done)

    def _apply_readout(self):
        mode = self._readout_want  # the newest wish: older queued jobs all apply the same one
        units = {"--live": (self.WIFI_HUD_UNIT, self.WIFI_HUD), "--minimap": (self.MINIMAP_UNIT, self.MINIMAP)}
        unit, path = units.get(mode, (None, None))
        user_systemctl("stop", *[u for u, _p in units.values() if u != unit])
        if not unit or user_systemctl("is-active", "--quiet", unit).returncode == 0:
            return None
        user_systemctl("reset-failed", unit)
        r = subprocess.run(["systemd-run", "--user", "--quiet", "--collect", f"--unit={unit}",
                            "--property=After=steamvr.service", "--property=PartOf=steamvr.service",
                            kitchen_timer.BIN, mode, path, str(os.getpid())],
                           capture_output=True, text=True, timeout=30)
        return None if r.returncode == 0 else ((r.stderr or r.stdout).strip()[-200:] or "systemd-run failed")

    def _readout_done(self, error):
        if not error or self._readout_want is None:
            return
        where = self.house_progress if (self.house_rec and self.house_rec.running) else self.wifi_progress
        set_result(where, f"The readout in your view didn't start: {error}", False)

    def live_hud(self):
        """One renderer for the in-view readout while mapping (a new one every second meant a full
        layout pass each time)."""
        if self._live_hud is None:
            self._live_hud = HudRenderer().__enter__()
        return self._live_hud

    def close_live_hud(self):
        if self._live_hud is not None:
            self._live_hud.__exit__()
            self._live_hud = None

    def build_wifi_page(self):
        self.wifi_rec = None
        self.wifi_target = "wifi"
        self._wifi_starting = False
        self._readout_want = None
        box = add_class(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18), "page")
        head = Gtk.Box(spacing=18)
        back = Gtk.Button(label="‹ Back to Signal")
        back.connect("clicked", lambda *_: self.show_page("Signal"))
        head.pack_start(back, False, False, 0)
        head.pack_start(label("Play-space signal map", "page-title"), False, False, 0)
        box.pack_start(head, False, False, 0)
        row = Gtk.Box(spacing=26)
        self.wifi_img = Gtk.Image()
        row.pack_start(self.wifi_img, False, False, 0)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        side.pack_start(label("Show signal of", "desc"), False, False, 0)
        self.wifi_target_seg = Segmented(list(wifi_map.TARGETS.items()), self.on_wifi_target)
        self.wifi_target_seg.set_value("wifi")
        side.pack_start(self.wifi_target_seg, False, False, 0)
        self.wifi_now = label("", "big", markup=True)
        side.pack_start(self.wifi_now, False, False, 0)
        self.wifi_now_desc = label("", "status", wrap=True)
        side.pack_start(self.wifi_now_desc, False, False, 0)
        acts = Gtk.Box(spacing=10, homogeneous=True)
        self.wifi_btn = add_class(Gtk.Button(label="Start mapping"), "primary")
        self.wifi_btn.connect("clicked", self.on_wifi_button)
        acts.pack_start(self.wifi_btn, True, True, 0)
        acts.pack_start(ConfirmButton("Clear map", "Tap again to clear", self.on_wifi_clear), True, True, 0)
        side.pack_start(acts, False, False, 0)
        self.wifi_progress = label("", "status", wrap=True)
        side.pack_start(self.wifi_progress, False, False, 0)
        self.wifi_result = label("", "status", wrap=True)
        side.pack_start(self.wifi_result, False, False, 0)
        side.pack_start(label("Walk slowly around your play space at the height you play, for a minute or two. "
                              "Both links are measured at once; a readout floats in your view; it stops by "
                              "itself after 5 minutes. For streaming VR, aim for -60 dBm or better.",
                              "desc", wrap=True), False, False, 0)
        row.pack_start(side, True, True, 0)
        box.pack_start(row, False, False, 0)
        return box

    def show_wifi_page(self):
        self.show_page("WifiMap")

    def on_wifi_target(self, target):
        self.wifi_target = target
        if self.wifi_rec and self.wifi_rec.running:
            self.render_wifi_hud()  # the readout in view follows the choice at once
        self.refresh_wifi(full=True)

    def refresh_wifi(self, full=False):
        rec = self.wifi_rec
        if rec and rec.running:
            self.show_wifi_now(rec.latest, rec.latest_dongle)
            m = {"bounds": rec.bounds, "samples": list(rec.samples)}
            el = int(rec.elapsed())
            set_info(self.wifi_progress, f"Mapping: {el // 60}:{el % 60:02d} · {len(rec.samples)} readings"
                     + tracking_note(rec))
            self.wifi_img.set_from_pixbuf(self.wifi_svg(m, self.wifi_target))
            return
        self.read_signals(self.show_wifi_now)
        if full:
            m = wifi_map.last_map()
            self.wifi_img.set_from_pixbuf(self.wifi_svg(m or {}, self.wifi_target))
            self.show_wifi_result(m)

    def show_wifi_now(self, wifi, dongle):
        s = wifi if self.wifi_target == "wifi" else dongle
        if s and s.get("dbm") is not None:
            word, colour = wifi_map.quality(s["dbm"])
            self.wifi_now.set_markup(f'<span foreground="{colour}">{s["dbm"]:.0f} dBm</span>')
            where = (f"{s['ssid']} ({wifi_map.band(s['freq'])})" if self.wifi_target == "wifi"
                     else "PC dongle, 6 GHz")
            self.wifi_now_desc.set_text(f"{word.capitalize()} · {s.get('tx') or 0:.0f} Mbit/s · {where}")
        elif self.wifi_target == "wifi":
            self.wifi_now.set_markup('<span foreground="#8a96a3">No Wi-Fi</span>')
            self.wifi_now_desc.set_text("The Frame isn't connected to a Wi-Fi network.")
        else:
            self.wifi_now.set_markup('<span foreground="#8a96a3">No dongle</span>')
            self.wifi_now_desc.set_text("The PC's dongle isn't linked right now (PC off or asleep, or Steam closed).")

    def show_wifi_result(self, m):
        if not m:
            self.wifi_result.set_text("")
            return
        r = (m.get("links") or {}).get(self.wifi_target) or (m if self.wifi_target == "wifi" else {})
        if not r.get("best"):
            self.wifi_result.set_text(f"No {wifi_map.TARGETS[self.wifi_target]} readings in this map." +
                                      (" Map again with the PC's dongle on." if self.wifi_target == "dongle"
                                       else ""))
            return
        b, w = r["best"], r["worst"]
        when = time.strftime("%b %d, %I:%M %p", time.localtime(m.get("saved", 0)))
        self.wifi_result.set_text(
            f"Map from {when}:\n"
            f"Best spot: {b['dbm']:.0f} dBm ({wifi_map.quality(b['dbm'])[0]}), {b['tx']} Mbit/s, "
            f"{wifi_map.where(b['at'])}\n"
            f"Weakest: {w['dbm']:.0f} dBm, {wifi_map.where(w['at'])}\nAverage: {r['avg']:.0f} dBm")

    def on_wifi_button(self, *_):
        if self.wifi_rec and self.wifi_rec.running:
            self.stop_wifi_mapping()
            return
        if (self.house_rec and self.house_rec.running) or self._house_starting:
            set_result(self.wifi_progress, "Stop the whole-house walk first.", False)
            return
        if self._wifi_starting:
            return
        self._wifi_starting = True
        self.wifi_btn.set_sensitive(False)
        self.wifi_result.set_text("")
        set_info(self.wifi_progress, "Starting…")
        rec = wifi_map.Recorder()

        def started(result):
            self._wifi_starting = False
            self.wifi_btn.set_sensitive(True)
            if isinstance(result, Exception):
                set_result(self.wifi_progress, f"Couldn't start: {result}", False)
                return
            if getattr(self, "_closing", False):
                rec.stop()
                return
            self.wifi_rec = rec
            self.wifi_btn.set_label("Stop mapping")
            set_info(self.wifi_progress, "Mapping: walk slowly around your play space.")
            self.render_wifi_hud()
            self.set_readout("--live")
            GLib.timeout_add(1000, safe(self.tick_wifi), rec)
        in_background(rec.start, started)  # reads the boundary from SteamVR: a second or two

    def on_wifi_clear(self):
        if self.wifi_rec and self.wifi_rec.running:
            self.stop_wifi_mapping()
        wifi_map.clear()
        set_info(self.wifi_progress, "Cleared. Press Start mapping to map from scratch.")
        self.wifi_img.set_from_pixbuf(self.wifi_svg({}, self.wifi_target))
        self.show_wifi_result(None)

    def tick_wifi(self, rec):
        if rec is not self.wifi_rec:
            return False  # that mapping session is over
        if not rec.running:  # stopped by itself (5 min)
            self.stop_wifi_mapping()
            return False
        self.render_wifi_hud()
        if self.focused and self.stack.get_visible_child_name() == "WifiMap":
            self.refresh_wifi()
        return True

    def render_wifi_hud(self):
        """The readout in your view: the chosen link's strength, big (titles stay short: the popup
        shortens anything over about 22 characters)."""
        rec = self.wifi_rec
        dongle = self.wifi_target == "dongle"
        s = (rec.latest_dongle if dongle else rec.latest) if rec else None
        name = "Dongle" if dongle else "Wi-Fi"
        if s and s.get("dbm") is not None:
            title, big = f"{name} · {wifi_map.quality(s['dbm'])[0]}", f"{s['dbm']:.0f} dBm"
        else:
            title, big = f"{name} map", "…"
        try:
            self.live_hud().render(title, big, self.WIFI_HUD)
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def stop_wifi_mapping(self):
        rec, self.wifi_rec = self.wifi_rec, None
        self.set_readout(None)
        self.close_live_hud()
        self.wifi_btn.set_label("Start mapping")
        if not rec:
            return
        m = rec.stop()
        n = len(m.get("samples", []))
        spots = ((m.get("links") or {}).get(self.wifi_target) or {}).get("cells", 0)
        if not n:
            set_result(self.wifi_progress, "No readings: tracking stopped (is SteamVR running?)." if rec.ended
                       else "No readings: the headset wasn't tracking. Wear it while you walk.", False)
        elif spots:
            set_result(self.wifi_progress, f"Done: {n} readings in {spots} spots.", True)
        else:
            set_result(self.wifi_progress, f"Done: {n} readings, none of them {wifi_map.TARGETS[self.wifi_target]}: "
                                           "show the other link above.", True)
        self.wifi_img.set_from_pixbuf(self.wifi_svg(m, self.wifi_target))
        self.show_wifi_result(m)

    @staticmethod
    def wifi_svg(m, target="wifi", w=880, h=700):
        """Heatmap of one link's strength over the boundary: 0.5 m squares on the colour-blind safe
        scale (bright yellow strong → dark purple weak)."""
        bounds = m.get("bounds") or []
        cells = wifi_map.grid(m.get("samples") or [], target)
        bg = "#141b24"
        xs = [p[0] for p in bounds] + [(i + d) * wifi_map.CELL for (i, _j) in cells for d in (0, 1)] + [0]
        zs = [p[1] for p in bounds] + [(j + d) * wifi_map.CELL for (_i, j) in cells for d in (0, 1)] + [0]
        if len(xs) < 2 or (max(xs) - min(xs) < 0.1 and max(zs) - min(zs) < 0.1):
            svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">'
                   f'<rect width="{w}" height="{h}" rx="14" fill="{bg}"/>'
                   f'<text x="{w / 2}" y="{h / 2}" fill="#9fb3c8" font-size="28" text-anchor="middle" '
                   f'font-family="sans-serif">No map yet: press Start mapping and walk around</text></svg>')
        else:
            minx, maxx, minz, maxz = min(xs), max(xs), min(zs), max(zs)
            pad, foot = 28, 70
            s = min((w - 2 * pad) / max(maxx - minx, 0.5), (h - 2 * pad - foot) / max(maxz - minz, 0.5))
            ox = (w - (maxx - minx) * s) / 2
            oz = (h - foot - (maxz - minz) * s) / 2

            def P(x, z):
                return ox + (x - minx) * s, oz + (z - minz) * s
            parts = [f'<rect width="{w}" height="{h}" rx="14" fill="{bg}"/>']
            for (i, j), (dbm, _tx, _n) in cells.items():
                x0, z0 = P(i * wifi_map.CELL, j * wifi_map.CELL)
                parts.append(f'<rect x="{x0:.1f}" y="{z0:.1f}" width="{wifi_map.CELL * s:.1f}" '
                             f'height="{wifi_map.CELL * s:.1f}" fill="{wifi_map.fill(dbm)}" fill-opacity="0.9"/>')
            if len(bounds) >= 3:
                poly = " ".join(f"{px:.1f},{pz:.1f}" for px, pz in (P(x, z) for x, z in bounds))
                parts.append(f'<polygon points="{poly}" fill="none" stroke="{ACCENT}" stroke-width="4" '
                             f'stroke-linejoin="round"/>')
            sx, sz = P(0, 0)  # where the room starts, facing forward (-z)
            parts.append(f'<line x1="{sx:.1f}" y1="{sz:.1f}" x2="{sx:.1f}" y2="{sz - 34:.1f}" stroke="#ffffff" '
                         f'stroke-width="5" stroke-linecap="round"/><circle cx="{sx:.1f}" cy="{sz:.1f}" r="9" '
                         f'fill="#ffffff"/>')
            summ = wifi_map.summarise_one(m, target)
            if summ.get("best"):
                bx, bz = P(*summ["best"]["at"])
                parts.append(f'<circle cx="{bx:.1f}" cy="{bz:.1f}" r="{max(14, wifi_map.CELL * s * 0.45):.1f}" '
                             f'fill="none" stroke="#ffffff" stroke-width="5"/>'
                             f'<text x="{bx:.1f}" y="{bz - wifi_map.CELL * s * 0.6 - 6:.1f}" fill="#ffffff" '
                             f'font-size="24" font-weight="bold" text-anchor="middle" font-family="sans-serif">'
                             f'Best</text>')
            parts.append(wifi_map.legend_svg(pad + 8, h - 52, w - 2 * pad - 16))
            svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">{"".join(parts)}</svg>'
        loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
        loader.write(svg.encode())
        loader.close()
        return loader.get_pixbuf()

    # ==================================================== whole-house map
    MAP_W, MAP_H = 820, 640  # house map picture, pixels
    TOOL_HINTS = {"move": "Move: drag the map", "zoom": "Zoom: tap to zoom in there, right-click to zoom out",
                  "labels": "Labels: tap to {mode}", "signal": "Signal: tap a spot to see its strength"}
    TOOL_CURSORS = {"move": "grab", "zoom": "zoom-in", "labels": "cell", "signal": "crosshair"}

    def build_house_page(self):
        self.house_rec = None
        self.house_watch = None   # PoseWatcher while the page is open and no walk runs
        self._house_starting = False
        self._watch_started = 0.0
        self._house_ticks = 0
        self._you_floor = None    # the floor you were last seen on while walking
        self._you_drawn = None    # where the "You" marker was last drawn (x, z, heading°)
        self._floors_shown = None
        self.floor_seg = None
        self._inspect = None
        recovered = house_map.recover() if "--snapshot" not in sys.argv else 0  # a walk cut short last time
        self.house_m = house_map.load()
        self.house_floor = 0
        self.house_target = "wifi"
        self._house_xf = None
        self.house_prefs = house_map.prefs()
        self.house_view = {"angle": float(self.house_prefs.get("angle", 0)), "zoom": 1.0, "pan": [0.0, 0.0]}
        self._drag = None
        self._drag_redraw = False
        box = add_class(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16), "page")
        head = Gtk.Box(spacing=18)
        back = Gtk.Button(label="‹ Back to Signal")
        back.connect("clicked", lambda *_: self.show_page("Signal"))
        head.pack_start(back, False, False, 0)
        head.pack_start(label("Whole-house map", "page-title"), False, False, 0)
        box.pack_start(head, False, False, 0)
        row = Gtk.Box(spacing=24)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.house_floor_box = Gtk.Box()
        left.pack_start(self.house_floor_box, False, False, 0)
        tools = Gtk.Box(spacing=8, homogeneous=True)  # what dragging / tapping the map does
        self.map_tool = "move"
        self.map_tool_btns = {}
        for tool, icons, text in (("move", ["deckhand-move"], "Move"),
                                  ("zoom", ["zoom-in"], "Zoom"),
                                  ("labels", ["tag", "bookmark-new"], "Labels"),
                                  ("signal", ["network-wireless-signal-excellent-symbolic", "network-wireless"],
                                   "Signal")):
            b = Gtk.Button()
            inner = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
            inner.pack_start(icon_image(icons, 34), False, False, 0)
            inner.pack_start(label(text), False, False, 0)
            b.add(inner)
            b.connect("clicked", lambda _b, t=tool: self.set_map_tool(t))
            tools.pack_start(b, True, True, 0)
            self.map_tool_btns[tool] = b
        left.pack_start(tools, False, False, 0)
        ev = Gtk.EventBox()
        self.house_ev = ev
        ev.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK |
                      Gdk.EventMask.BUTTON1_MOTION_MASK | Gdk.EventMask.SCROLL_MASK)
        self.house_img = Gtk.Image()
        ev.add(self.house_img)
        ev.connect("button-press-event", self.on_map_press)
        ev.connect("motion-notify-event", self.on_map_drag)
        ev.connect("button-release-event", self.on_map_release)
        ev.connect("scroll-event", self.on_map_scroll)
        ev.connect("realize", lambda *_: self.set_map_cursor())
        left.pack_start(ev, False, False, 0)
        view = Gtk.Box(spacing=8, homogeneous=True)  # view buttons: rotate, zoom, find me, reset
        for icons, text, act in ((["object-rotate-left"], "Left", lambda: self.map_rotate(-15)),
                                 (["object-rotate-right"], "Right", lambda: self.map_rotate(15)),
                                 (["zoom-out"], "Out", lambda: self.map_zoom_at(None, 1 / 1.4)),
                                 (["zoom-in"], "In", lambda: self.map_zoom_at(None, 1.4)),
                                 (["find-location", "mark-location"], "Me", self.map_center_me),
                                 (["view-restore", "edit-reset", "zoom-original"], "Reset", self.map_reset)):
            b = add_class(Gtk.Button(), "seg")
            inner = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
            inner.pack_start(icon_image(icons, 30), False, False, 0)
            inner.pack_start(label(text), False, False, 0)
            b.add(inner)
            b.set_tooltip_text({"Left": "Rotate left", "Right": "Rotate right", "Out": "Zoom out", "In": "Zoom in",
                                "Me": "Center on me", "Reset": "Reset the view"}[text])
            b.connect("clicked", lambda _b, a=act: a())
            view.pack_start(b, True, True, 0)
        left.pack_start(view, False, False, 0)
        GLib.idle_add(lambda: (self.set_map_tool("move", redraw=False), False)[1])
        row.pack_start(left, False, False, 0)

        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        side.pack_start(label("Show signal of", "desc"), False, False, 0)
        self.house_target_seg = Segmented(list(house_map.TARGETS.items()), self.on_house_target)
        self.house_target_seg.set_value("wifi")
        side.pack_start(self.house_target_seg, False, False, 0)
        self.house_now = label("", "big", markup=True)
        side.pack_start(self.house_now, False, False, 0)
        self.house_now_desc = label("", "status", wrap=True, markup=True)
        side.pack_start(self.house_now_desc, False, False, 0)
        acts = Gtk.Box(spacing=10, homogeneous=True)
        self.house_btn = add_class(Gtk.Button(label="Start walking"), "primary")
        self.house_btn.connect("clicked", self.on_house_button)
        acts.pack_start(self.house_btn, True, True, 0)
        acts.pack_start(ConfirmButton("Clear map", "Tap again to clear", self.on_house_clear), True, True, 0)
        side.pack_start(acts, False, False, 0)
        self.minimap_switch = Gtk.Switch(active=self.house_prefs["minimap"])
        self.minimap_switch.connect("notify::active", self.on_house_pref, "minimap")
        side.pack_start(switch_row("Mini-map in view", label("In the middle of your view while you walk, like "
                                                             "in a game; turns with you.", "desc", wrap=True),
                                   self.minimap_switch), False, False, 0)
        self.house_progress = label(
            f"Recovered a walk that was cut short ({recovered} positions) and added it to the map." if recovered
            else "Walk every room with the headset on (passthrough helps). Wi-Fi and the Steam dongle are "
                 "both measured. Walks add up; Clear map starts fresh.", "desc", wrap=True)
        side.pack_start(self.house_progress, False, False, 0)
        self.house_result = label("", "status", wrap=True)
        side.pack_start(self.house_result, False, False, 0)
        side.pack_start(label("Labels (picking one switches the map to the Labels tool)", "card-title", wrap=True),
                        False, False, 0)
        self._label_sel = None    # index of the label being moved
        self._label_undo = None   # labels before the last change
        self.label_mode = Segmented([("add", "Add"), ("move", "Move"), ("rename", "Rename"),
                                     ("delete", "Delete")], self.on_label_mode, per_row=2)
        self.label_mode.set_value("add")
        side.pack_start(self.label_mode, False, False, 0)
        self.house_label = Segmented([(n, n) for n in house_map.LABELS], self.on_label_name, per_row=3)
        self.house_label.set_value("Router")
        side.pack_start(self.house_label, False, False, 0)
        self.label_custom = Gtk.Entry(placeholder_text="Or type your own name…", max_length=24)
        self.label_custom.connect("changed", self.on_label_custom)
        side.pack_start(self.label_custom, False, False, 0)
        here = add_class(Gtk.Button(label="Label where I'm standing"), "primary")
        here.connect("clicked", self.on_label_here)
        side.pack_start(here, False, False, 0)
        self.label_status = label("Add: pick a name, then tap the map where it is (also while walking).",
                                  "status", wrap=True)
        side.pack_start(self.label_status, False, False, 0)
        acts2 = Gtk.Box(spacing=10, homogeneous=True)
        undo = Gtk.Button(label="Undo")
        undo.connect("clicked", self.on_house_undo_label)
        acts2.pack_start(undo, True, True, 0)
        side.pack_start(acts2, False, False, 0)
        # The controls scroll on their own, so the map stays in view while you label it.
        side_sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        side_sw.set_size_request(-1, 860)
        side_sw.add(side)
        row.pack_start(side_sw, True, True, 0)
        box.pack_start(row, False, False, 0)
        return box

    def show_house_page(self):
        self.show_page("House")

    def house_watch_start(self):
        """Follows the headset for the "You" marker while this page is open (a walk does it itself)."""
        if (self.house_rec and self.house_rec.running) or self._house_starting or "--snapshot" in sys.argv:
            return
        w = self.house_watch
        if w and w.alive():
            return
        if w:  # its tracking tool ended (SteamVR stopped or restarted): try again now and then
            if time.monotonic() - self._watch_started < 15:
                return
            w.stop()
            self.house_watch = None
        self._watch_started = time.monotonic()
        try:
            self.house_watch = house_map.PoseWatcher().start()
        except Exception:  # noqa: BLE001  (no helper: just no marker)
            self.house_watch = None

    def house_watch_stop(self):
        if self.house_watch:
            self.house_watch.stop()
            self.house_watch = None

    def house_you(self):
        """(x, y, z, (fx, fz)) of the headset for the map, or None."""
        src = self.house_rec if (self.house_rec and self.house_rec.running) else self.house_watch
        if src and src.pose:
            return (*src.pose, src.heading)
        return None

    def on_house_target(self, target):
        self.house_target = target
        if self.house_rec and self.house_rec.running:  # the mini-map / readout follow the choice at once
            self.render_minimap() if self.house_prefs["minimap"] else self.render_house_hud()
        self.refresh_house(full=True)

    def refresh_house(self, full=False):
        rec = self.house_rec
        if rec and rec.running:
            self.show_house_now(rec.latest, rec.latest_dongle)
            el = int(rec.elapsed())
            set_info(self.house_progress, f"Walking: {el // 60}:{el % 60:02d} · {rec.distance:.0f} m · "
                                          f"{len(rec.samples)} positions" + tracking_note(rec))
            self.draw_house(house_map.add_walk(self.house_m, rec), follow=True)
            return
        self.read_signals(self.show_house_now)
        self.house_watch_start()
        if full or self.you_moved():  # the "You" marker moves with you
            self.draw_house(self.house_m or {})
        if full:
            self.show_house_result()

    def show_house_now(self, wifi, dongle):
        sel = wifi if self.house_target == "wifi" else dongle
        if sel and sel.get("dbm") is not None:
            self.house_now.set_markup(f'<span foreground="{wifi_map.quality(sel["dbm"])[1]}">'
                                      f'{sel["dbm"]:.0f} dBm</span>')
        else:
            self.house_now.set_markup('<span foreground="#8a96a3">'
                                      f'{"No Wi-Fi" if self.house_target == "wifi" else "No dongle"}</span>')

        def line(name, s, missing):
            if not s or s.get("dbm") is None:
                return f"{name}: {missing}"
            word, colour = wifi_map.quality(s["dbm"])
            return (f'{name}: <span foreground="{colour}">{s["dbm"]:.0f} dBm</span> {word}, '
                    f'{s.get("tx") or 0:.0f} Mbit/s')
        self.house_now_desc.set_markup(
            line("Home Wi-Fi", wifi, "not connected") + "\n" +
            line("Steam dongle", dongle, "not connected (PC dongle off or asleep)"))

    def draw_house(self, m, follow=False):
        """Draws the map. follow=True (while walking): switches to a floor you just walked onto."""
        self._house_drawn = m
        samples = m.get("samples") or []
        fl = house_map.floors(samples) if samples else [0]
        you = self.house_you()
        if follow and you and samples:
            f = house_map.floor_of(you[1], house_map.floor_base(samples))
            if f != self._you_floor:
                self._you_floor = f
                if f in fl and f != self.house_floor:
                    self.house_floor = f
                    self._label_sel = self._inspect = None
        if self.house_floor not in fl:
            self.house_floor = fl[0]
        self.update_floor_picker(fl)
        tool = getattr(self, "map_tool", "move")
        hint = self.TOOL_HINTS[tool].format(mode={"add": "add a label", "move": "pick a label, then tap where it goes",
                                                  "rename": "rename a label", "delete": "delete a label"}
                                            .get(getattr(self, "label_mode", None) and self.label_mode.value, "label"))
        pix, self._house_xf = self.house_svg(m, self.house_floor, self.house_target,
                                             selected=getattr(self, "_label_sel", None),
                                             view=self.house_view, you=you,
                                             w=self.MAP_W, h=self.MAP_H, hint=hint, inspect=self._inspect)
        self._you_drawn = self.you_key(you)
        self.house_img.set_from_pixbuf(pix)

    def update_floor_picker(self, fl):
        """Floor buttons, rebuilt only when the floors change (rebuilding them on every redraw could
        swallow a tap)."""
        if fl != self._floors_shown:
            self._floors_shown = list(fl)
            for child in self.house_floor_box.get_children():
                self.house_floor_box.remove(child)
            self.floor_seg = None
            if len(fl) > 1:
                self.floor_seg = Segmented([(f, house_map.floor_name(f)) for f in fl], self.on_house_floor,
                                           per_row=3)
                self.house_floor_box.pack_start(self.floor_seg, True, True, 0)
                self.house_floor_box.show_all()
        if self.floor_seg and self.floor_seg.value != self.house_floor:
            self.floor_seg.set_value(self.house_floor)

    def on_house_floor(self, f):
        self.house_floor = f
        self._label_sel = self._inspect = None
        self.redraw_house()

    @staticmethod
    def you_key(you):
        return None if not you else (you[0], you[2], math.degrees(math.atan2(you[3][0], you[3][1])))

    def you_moved(self):
        """True when the "You" marker moved enough (20 cm or 10°) to be worth redrawing the map."""
        now, last = self.you_key(self.house_you()), self._you_drawn
        if now is None or last is None:
            return now != last
        turned = abs((now[2] - last[2] + 180) % 360 - 180)
        return math.hypot(now[0] - last[0], now[1] - last[1]) > 0.2 or turned > 10

    # ---- map view: rotate / zoom / pan (drag) / center on me / reset
    def map_rotate(self, angle):
        """Turns the map. Zoomed in or moved, the spot in the middle stays in the middle."""
        v, xf = self.house_view, self._house_xf
        if xf and (v["zoom"] != 1.0 or v["pan"] != [0.0, 0.0]):
            v["anchor"] = self.house_view_metres(xf, xf["w"] / 2, xf["hm"] / 2)
        v["angle"] = (v["angle"] + angle) % 360
        self.house_prefs["angle"] = v["angle"]
        house_map.save_prefs(self.house_prefs)
        self.redraw_house()

    def map_reset(self):
        self.house_view.update(angle=0.0, zoom=1.0, pan=[0.0, 0.0])
        self.house_view.pop("anchor", None)
        self._inspect = None
        self.house_prefs["angle"] = 0.0
        house_map.save_prefs(self.house_prefs)
        self.redraw_house()

    def map_center_me(self):
        """Centres the map on you (on the floor you're on)."""
        you = self.house_you()
        if not you:
            set_result(self.label_status, "Your position isn't known right now (headset not tracking?).", False)
            return
        m = getattr(self, "_house_drawn", None) or self.house_m or {}
        samples = m.get("samples") or []
        if samples:
            f = house_map.floor_of(you[1], house_map.floor_base(samples))
            if f != self.house_floor and f in house_map.floors(samples):
                self.house_floor = f
                self._label_sel = self._inspect = None
        self.house_view["anchor"] = (you[0], you[2])
        self.redraw_house()

    def redraw_house(self):
        m = getattr(self, "_house_drawn", None)
        self.draw_house(m if m is not None else (self.house_m or {}))

    @staticmethod
    def house_view_point(xf, x, z, pan=True):
        """Metres → map pixels with the view's rotation, zoom and (optionally) pan."""
        dx, dz = x - xf["cx"], z - xf["cz"]
        rx = dx * xf["cos"] - dz * xf["sin"]
        rz = dx * xf["sin"] + dz * xf["cos"]
        px, py = xf["pan"] if pan else (0.0, 0.0)
        return xf["w"] / 2 + rx * xf["s"] + px, xf["hm"] / 2 + rz * xf["s"] + py

    @staticmethod
    def house_view_metres(xf, sx, sy):
        """Map pixels → metres (inverse of house_view_point)."""
        rx = (sx - xf["w"] / 2 - xf["pan"][0]) / xf["s"]
        rz = (sy - xf["hm"] / 2 - xf["pan"][1]) / xf["s"]
        return (xf["cx"] + rx * xf["cos"] + rz * xf["sin"], xf["cz"] - rx * xf["sin"] + rz * xf["cos"])

    def _map_xy(self, event):
        pix = self.house_img.get_pixbuf()
        alloc = self.house_img.get_allocation()
        return (event.x - (alloc.width - pix.get_width()) / 2, event.y - (alloc.height - pix.get_height()) / 2)

    def set_map_tool(self, tool, redraw=True):
        self.map_tool = tool
        for t, b in self.map_tool_btns.items():
            (b.get_style_context().add_class if t == tool else b.get_style_context().remove_class)("selected")
        if tool != "signal":
            self._inspect = None
        if tool != "labels":
            self._label_sel = None
        self.set_map_cursor()
        if redraw:
            self.redraw_house()

    def set_map_cursor(self, name=None):
        win = self.house_ev.get_window() if getattr(self, "house_ev", None) else None
        if not win:
            return
        try:
            cur = Gdk.Cursor.new_from_name(win.get_display(), name or self.TOOL_CURSORS.get(self.map_tool, "default"))
        except Exception:  # noqa: BLE001  (cursor theme without that name)
            cur = None
        win.set_cursor(cur)

    def map_zoom_at(self, point, k):
        """Zooms by k keeping the map spot under `point` (map pixels) still; None = the middle."""
        xf = self._house_xf
        v = self.house_view
        new = max(0.4, min(8.0, v["zoom"] * k))
        k = new / v["zoom"]
        if xf and k != 1.0:
            sx, sy = point if point else (xf["w"] / 2, xf["hm"] / 2)
            cx, cy = xf["w"] / 2, xf["hm"] / 2
            v["pan"] = [sx - cx - (sx - cx - v["pan"][0]) * k, sy - cy - (sy - cy - v["pan"][1]) * k]
        v["zoom"] = new
        self.redraw_house()

    def on_map_press(self, _w, event):
        if event.button in (1, 3) and self._house_xf:
            x, y = self._map_xy(event)
            self._drag = {"x": x, "y": y, "pan": list(self.house_view["pan"]), "moved": False,
                          "button": event.button}
            if self.map_tool in ("move", "zoom") and event.button == 1:
                self.set_map_cursor("grabbing")
        return True

    def on_map_drag(self, _w, event):
        d = self._drag
        if not d or self.map_tool not in ("move", "zoom") or d["button"] != 1:
            return bool(d)
        x, y = self._map_xy(event)
        if not d["moved"] and math.hypot(x - d["x"], y - d["y"]) < (4 if self.map_tool == "move" else 18):
            return True  # a tap, not a drag (yet)
        d["moved"] = True
        self.house_view["pan"] = [d["pan"][0] + x - d["x"], d["pan"][1] + y - d["y"]]
        if not self._drag_redraw:  # redraw at most ~12 times a second while dragging
            self._drag_redraw = True
            GLib.timeout_add(80, self._drag_tick)
        return True

    def _drag_tick(self):
        self._drag_redraw = False
        self.redraw_house()
        return False

    def on_map_release(self, _w, event):
        d, self._drag = self._drag, None
        if not d:
            return False
        self.set_map_cursor()
        x, y = self._map_xy(event)
        tap = not d["moved"] and math.hypot(x - d["x"], y - d["y"]) < 24  # laser pointers wobble
        if d["moved"]:
            self.redraw_house()
        elif not tap:
            pass
        elif self.map_tool == "zoom":
            self.map_zoom_at((x, y), 1.6 if d["button"] == 1 else 1 / 1.6)
        elif self.map_tool == "labels" and d["button"] == 1:
            self.on_house_tap(event)
        elif self.map_tool == "signal" and d["button"] == 1:
            self.inspect_at(x, y)
        return True

    def on_map_scroll(self, _w, event):
        point = self._map_xy(event)
        if event.direction == Gdk.ScrollDirection.UP:
            self.map_zoom_at(point, 1.25)
        elif event.direction == Gdk.ScrollDirection.DOWN:
            self.map_zoom_at(point, 1 / 1.25)
        return True

    def inspect_at(self, sx, sy):
        """Signal tool: both links' strength in the 0.5 m square tapped (or the nearest measured one)."""
        xf = self._house_xf
        m = getattr(self, "_house_drawn", None) or self.house_m
        if not xf or not m or not m.get("samples"):
            return
        x, z = self.house_view_metres(xf, sx, sy)
        found = {}
        for target in house_map.TARGETS:
            cs = {(i, j): v for (f, i, j), v in house_map.cells(m["samples"], target).items() if f == self.house_floor}
            key = (int(x // house_map.CELL), int(z // house_map.CELL))
            if key in cs:
                found[target] = (cs[key], 0.0)
                continue
            best = None
            for (i, j), v in cs.items():
                d = math.hypot((i + 0.5) * house_map.CELL - x, (j + 0.5) * house_map.CELL - z)
                if d <= 1.5 and (best is None or d < best[1]):
                    best = (v, d)
            found[target] = best
        self._inspect = {"x": x, "z": z, "found": found, "first": self.house_target}
        self.redraw_house()

    @staticmethod
    def house_svg(m, floor, target="wifi", w=820, h=640, selected=None, view=None, you=None, hint="",
                  inspect=None):
        """Top-down map of one floor: 0.5 m squares by the chosen signal, the edge of the walked area
        (dashed), the path, labels, you, a scale bar and the signal legend. `view` rotates, zooms and
        pans it. Returns (pixbuf, transform) — the transform maps pixels back to metres for taps."""
        view = view or {"angle": 0.0, "zoom": 1.0, "pan": [0.0, 0.0]}
        samples = m.get("samples") or []
        base = house_map.floor_base(samples) if samples else 0
        on_floor = [s for s in samples if house_map.floor_of(s[1], base) == floor]
        walked = {(i, j) for f, i, j in house_map.footprint(samples) if f == floor} if samples else set()
        cs = {(i, j): v for (f, i, j), v in house_map.cells(samples, target).items() if f == floor} if samples else {}
        labels = [(i, l) for i, l in enumerate(m.get("labels", [])) if l.get("floor", 0) == floor]
        here = you if you and samples and house_map.floor_of(you[1], base) == floor else None
        bg = "#141b24"
        foot = 96
        hm = h - foot
        # The fit covers the walked squares, labels and the start, not you: the map doesn't rescale
        # as you move (you're kept at its edge when you're off it).
        xs = [(i + d) * house_map.CELL for (i, _j) in walked for d in (0, 1)] + [l["x"] for _i, l in labels] + [0]
        zs = [(j + d) * house_map.CELL for (_i, j) in walked for d in (0, 1)] + [l["z"] for _i, l in labels] + [0]
        xf = None
        if not walked:
            svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">'
                   f'<rect width="{w}" height="{h}" rx="14" fill="{bg}"/>'
                   f'<text x="{w / 2}" y="{h / 2}" fill="#9fb3c8" font-size="28" text-anchor="middle" '
                   f'font-family="sans-serif">No house map yet: press Start walking</text></svg>')
        else:
            th = math.radians(view["angle"])
            co, si = math.cos(th), math.sin(th)
            rxs = [x * co - z * si for x, z in zip(xs, zs)]  # turned by the view's angle
            rzs = [x * si + z * co for x, z in zip(xs, zs)]
            rcx, rcz = (max(rxs) + min(rxs)) / 2, (max(rzs) + min(rzs)) / 2
            cx, cz = rcx * co + rcz * si, -rcx * si + rcz * co  # the middle of the turned map, in metres
            pad = 30
            fit = min((w - 2 * pad) / (max(rxs) - min(rxs) + 1.0), (hm - 2 * pad) / (max(rzs) - min(rzs) + 1.0))
            s = fit * view["zoom"]
            xf = {"cx": cx, "cz": cz, "cos": co, "sin": si, "s": s, "pan": [0.0, 0.0], "w": w, "hm": hm}
            anchor = view.pop("anchor", None)
            if anchor:  # this spot (metres) goes in the middle: "Me", or keeping the middle while turning
                ax, ay = FrameSettings.house_view_point(xf, *anchor)
                view["pan"] = [w / 2 - ax, hm / 2 - ay]
            xf["pan"] = list(view["pan"])

            def P(x, z):
                return FrameSettings.house_view_point(xf, x, z)

            def poly(corners):
                return " ".join("%.1f,%.1f" % P(x, z) for x, z in corners)
            C = house_map.CELL
            parts = [f'<rect width="{w}" height="{h}" rx="14" fill="{bg}"/>',
                     f'<defs><clipPath id="mapclip"><rect x="0" y="0" width="{w}" height="{hm}" rx="14"/></clipPath>'
                     f'</defs><g clip-path="url(#mapclip)">']
            for (i, j) in walked:  # floor where you walked; grey where this signal wasn't measured
                v = cs.get((i, j))
                x0, z0 = i * C, j * C
                parts.append(f'<polygon points="{poly(((x0, z0), (x0 + C, z0), (x0 + C, z0 + C), (x0, z0 + C)))}" '
                             f'fill="{wifi_map.fill(v[0]) if v else "#2a3440"}" fill-opacity="0.9" '
                             f'stroke="{wifi_map.fill(v[0]) if v else "#2a3440"}" stroke-width="0.8"/>')
            for xa, za, xb, zb in house_map.outline_edges(samples, floor):  # edge of where you walked
                (ax, az), (bx, bz) = P(xa, za), P(xb, zb)
                parts.append(f'<line x1="{ax:.1f}" y1="{az:.1f}" x2="{bx:.1f}" y2="{bz:.1f}" stroke="#9fd8ff" '
                             f'stroke-opacity="0.5" stroke-width="2" stroke-dasharray="7 6"/>')
            seg, pts = None, []
            for smp in on_floor + [[0, 0, 0, None, None, -1]]:  # path, one polyline per segment
                x, z, sg = smp[0], smp[2], smp[5]
                if sg != seg and len(pts) > 1:
                    parts.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="#ffffff" '
                                 f'stroke-opacity="0.45" stroke-width="2"/>')
                if sg != seg:
                    pts, seg = [], sg
                px, pz = P(x, z)
                pts.append(f"{px:.1f},{pz:.1f}")
            if floor == 0:
                sx, sz = P(0, 0)
                parts.append(f'<circle cx="{sx:.1f}" cy="{sz:.1f}" r="7" fill="#ffffff"/>')
            for idx, l in labels:  # a dot, its name underneath; the one being moved gets a ring
                lx, lz = P(l["x"], l["z"])
                if idx == selected:
                    parts.append(f'<circle cx="{lx:.1f}" cy="{lz:.1f}" r="20" fill="none" stroke="#fde725" '
                                 f'stroke-width="5"/>')
                parts.append(f'<circle cx="{lx:.1f}" cy="{lz:.1f}" r="9" fill="{ACCENT}" stroke="#0e141b" '
                             f'stroke-width="3"/><text x="{lx:.1f}" y="{lz + 32:.1f}" fill="#ffffff" '
                             f'font-size="22" font-weight="bold" text-anchor="middle" font-family="sans-serif" '
                             f'stroke="#0e141b" stroke-width="5" paint-order="stroke">{html.escape(l["name"])}</text>')
            if here:  # you: a big arrow pointing where you look, with a ring and "You" (at the edge if
                ux, uz = P(here[0], here[2])  # you're off the map)
                off = not (40 <= ux <= w - 40 and 40 <= uz <= hm - 80)
                ux, uz = min(max(ux, 40), w - 40), min(max(uz, 40), hm - 80)
                fx, fz = here[3]
                hx, hy = fx * co - fz * si, fx * si + fz * co      # look direction on screen
                n = math.hypot(hx, hy) or 1.0
                hx, hy = hx / n, hy / n
                lx, ly = -hy, hx
                tip = (ux + hx * 30, uz + hy * 30)
                bl = (ux - hx * 16 + lx * 18, uz - hy * 16 + ly * 18)
                br = (ux - hx * 16 - lx * 18, uz - hy * 16 - ly * 18)
                notch = (ux - hx * 6, uz - hy * 6)
                parts.append(f'<circle cx="{ux:.1f}" cy="{uz:.1f}" r="40" fill="#fde725" fill-opacity="0.18" '
                             f'stroke="#fde725" stroke-width="3"/>'
                             f'<polygon points="{tip[0]:.1f},{tip[1]:.1f} {bl[0]:.1f},{bl[1]:.1f} '
                             f'{notch[0]:.1f},{notch[1]:.1f} {br[0]:.1f},{br[1]:.1f}" fill="#fde725" '
                             f'stroke="#0e141b" stroke-width="4" stroke-linejoin="round"/>'
                             f'<text x="{min(max(ux, 110), w - 110):.1f}" y="{uz + 66:.1f}" fill="#fde725" '
                             f'font-size="24" font-weight="bold" '
                             f'text-anchor="middle" font-family="sans-serif" stroke="#0e141b" stroke-width="6" '
                             f'paint-order="stroke">{"You (off map)" if off else "You"}</text>')
            if inspect:  # Signal tool: a pin and a callout with both links' numbers
                ix, iy = P(inspect["x"], inspect["z"])
                order = [inspect["first"]] + [t for t in house_map.TARGETS if t != inspect["first"]]
                rows, note = [], ""
                for t in order:
                    hit = inspect["found"].get(t)
                    if hit:
                        (dbm, tx, n), dist = hit
                        word, col = wifi_map.quality(dbm)
                        rows.append((house_map.TARGETS[t], f"{dbm:.0f} dBm", f"{word}, {tx:.0f} Mbit/s", col))
                        if dist > 0.01 and not note:
                            note = f"nearest reading {dist:.1f} m away"
                        elif not note:
                            note = f"{n} reading{'s' if n != 1 else ''} in this 0.5 m square"
                    else:
                        rows.append((house_map.TARGETS[t], "—", "no reading here", "#8a96a3"))
                bw, bh = 430, 172
                bx = min(max(ix - bw / 2, 10), w - bw - 10)
                by = iy - bh - 26 if iy - bh - 26 > 10 else iy + 26
                parts.append(f'<circle cx="{ix:.1f}" cy="{iy:.1f}" r="11" fill="#ffffff" stroke="#0e141b" '
                             f'stroke-width="4"/><circle cx="{ix:.1f}" cy="{iy:.1f}" r="4" fill="#0e141b"/>'
                             f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bw}" height="{bh}" rx="14" fill="#0e141b" '
                             f'fill-opacity="0.92" stroke="#ffffff" stroke-width="3"/>')
                for k, (name, num, sub, col) in enumerate(rows):
                    ty = by + 46 + k * 58
                    parts.append(f'<text x="{bx + 18:.1f}" y="{ty:.1f}" fill="#c9d6e3" font-size="22" '
                                 f'font-family="sans-serif">{name}</text>'
                                 f'<text x="{bx + 210:.1f}" y="{ty:.1f}" fill="{col}" font-size="32" font-weight="bold" '
                                 f'font-family="sans-serif">{num}</text>'
                                 f'<text x="{bx + 210:.1f}" y="{ty + 20:.1f}" fill="#9fb3c8" font-size="16" '
                                 f'font-family="sans-serif">{sub}</text>')
                if note:
                    parts.append(f'<text x="{bx + 18:.1f}" y="{by + bh - 14:.1f}" fill="#9fb3c8" font-size="16" '
                                 f'font-family="sans-serif">{note}</text>')
            parts.append("</g>")
            fits = [k for k in (0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100) if k * s <= 220]  # scale bar, top left
            metres = fits[-1] if fits else 0.05
            bar = metres * s
            unit = f"{metres:g} m"
            parts.append(f'<rect x="12" y="12" width="{bar + 80:.1f}" height="36" rx="8" fill="{bg}" '
                         f'fill-opacity="0.85"/><line x1="22" y1="30" x2="{22 + bar:.1f}" y2="30" stroke="#c9d6e3" '
                         f'stroke-width="4"/><text x="{bar + 30:.1f}" y="37" fill="#c9d6e3" font-size="19" '
                         f'font-family="sans-serif">{unit}</text>')
            # compass, top right: which way you faced where the map started
            ccx, ccy = w - 52, 52
            ax_, ay_ = 0 * co - (-1) * si, 0 * si + (-1) * co
            parts.append(f'<circle cx="{ccx}" cy="{ccy}" r="34" fill="{bg}" fill-opacity="0.85" stroke="#9fb3c8" '
                         f'stroke-width="2"/><line x1="{ccx - ax_ * 20:.1f}" y1="{ccy - ay_ * 20:.1f}" '
                         f'x2="{ccx + ax_ * 22:.1f}" y2="{ccy + ay_ * 22:.1f}" stroke="#ffffff" stroke-width="4" '
                         f'stroke-linecap="round"/><circle cx="{ccx + ax_ * 22:.1f}" cy="{ccy + ay_ * 22:.1f}" r="5" '
                         f'fill="#ffffff"/><text x="{ccx}" y="{ccy + 56}" fill="#9fb3c8" font-size="15" '
                         f'text-anchor="middle" font-family="sans-serif">start</text>')
            parts.append(f'<text x="{w / 2}" y="{h - 74}" fill="#c9d6e3" font-size="17" text-anchor="middle" '
                         f'font-family="sans-serif">{house_map.TARGETS[target]} · yellow arrow = you · {html.escape(hint)}'
                         f'</text>')
            parts.append(wifi_map.legend_svg(pad, h - 46, w - 2 * pad))
            svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">{"".join(parts)}</svg>'
        loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
        loader.write(svg.encode())
        loader.close()
        return loader.get_pixbuf(), xf

    def show_house_result(self):
        m = self.house_m
        if not m or not m.get("samples"):  # nothing walked yet (maybe labels only)
            self.house_result.set_text("")
            return
        sm = house_map.summary(m, self.house_target)
        when = time.strftime("%b %d, %I:%M %p", time.localtime(m.get("saved", 0)))
        walks = m.get("walks", 1)
        head = f"{walks} walk{'s' if walks != 1 else ''}, last {when}: {m.get('distance', 0):.0f} m\n"
        if not sm:
            name = house_map.TARGETS[self.house_target]
            self.house_result.set_text(head + f"No {name} readings in this map" +
                                       (": the dongle was off or asleep while walking." if
                                        self.house_target == "dongle" else "."))
            return
        self.house_result.set_text(
            head + f"Best: {sm['best']}\nWeakest: {sm['worst']}\nAverage {sm['avg']:.0f} dBm · "
            f"{sm['weak_share'] * 100:.0f}% of the walked area is weak or poor")

    def on_house_button(self, *_):
        if self.house_rec and self.house_rec.running:
            self.stop_house_walk()
            return
        if (self.wifi_rec and self.wifi_rec.running) or self._wifi_starting:
            set_result(self.house_progress, "Stop the play-space map first.", False)
            return
        if self._house_starting:
            return
        self._house_starting = True
        self.house_btn.set_sensitive(False)
        self.house_result.set_text("")
        set_info(self.house_progress, "Starting…")
        self.house_watch_stop()  # the walk follows the headset itself
        rec = house_map.HouseRecorder()
        segs = [s[house_map.SEG] for s in (self.house_m or {}).get("samples", [])]
        offset = max(segs) + 1 if segs else 0

        def started(result):
            self._house_starting = False
            self.house_btn.set_sensitive(True)
            if isinstance(result, Exception):
                set_result(self.house_progress, f"Couldn't start: {result}", False)
                return
            if getattr(self, "_closing", False):
                rec.stop()
                return
            self.house_rec = rec
            self._you_floor = None
            self._house_ticks = 0
            self.house_btn.set_label("Stop walking")
            set_info(self.house_progress, "Walking: go from room to room at your normal pace.")
            self.set_minimap(self.house_prefs["minimap"])
            GLib.timeout_add(1000, safe(self.tick_house), rec)
        in_background(lambda: rec.start(segment_offset=offset), started)

    def tick_house(self, rec):
        if rec is not self.house_rec:
            return False  # that walk is over
        if not rec.running:  # stopped by itself (an hour)
            self.stop_house_walk()
            return False
        if self.house_prefs["minimap"]:
            self.render_minimap()
        else:
            self.render_house_hud()
        self._house_ticks += 1
        if self._house_ticks % 3 == 0 and self.focused and self.stack.get_visible_child_name() == "House":
            self.refresh_house()  # redrawing the whole map every 3 s is plenty
        return True

    def render_house_hud(self):
        """Floating readout while walking (when the mini-map is off): the chosen link's strength, big."""
        rec = self.house_rec
        el = int(rec.elapsed()) if rec else 0
        sel = ((rec.latest_dongle if self.house_target == "dongle" else rec.latest) if rec else None)
        title = f"{house_map.TARGETS[self.house_target]} · {el // 60}:{el % 60:02d}"
        big = f"{sel['dbm']:.0f} dBm" if sel and sel.get("dbm") is not None else "…"
        try:
            self.live_hud().render(title, big, self.WIFI_HUD)
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def stop_house_walk(self):
        rec, self.house_rec = self.house_rec, None
        self.set_readout(None)
        self.close_live_hud()
        self.house_btn.set_label("Start walking")
        if not rec:
            return
        rec.stop()
        self.house_m = house_map.finish_walk(self.house_m, rec)
        n = len(rec.samples)
        if n:
            wifi = sum(1 for s in rec.samples if s[house_map.WIFI] is not None)
            dongle = sum(1 for s in rec.samples if s[house_map.DONGLE] is not None)
            missing = ("no signal readings (Wi-Fi and the PC dongle both off?)" if not (wifi or dongle) else
                       "no dongle readings (PC dongle off?)" if not dongle else
                       "no Wi-Fi readings" if not wifi else "")
            set_result(self.house_progress, f"Added: {rec.distance:.0f} m walked, {n} positions"
                       + (f", {missing}" if missing else "") + ". Walk more to add rooms, then label the map.", True)
        else:
            set_result(self.house_progress, "Nothing added: tracking stopped (is SteamVR running?)." if rec.ended
                       else "Nothing added: the headset wasn't tracking. Wear it while you walk.", False)
        if self.focused and self.stack.get_visible_child_name() == "House":
            self.house_watch_start()
        self.draw_house(self.house_m or {})
        self.show_house_result()

    def on_house_pref(self, switch, _pspec, key):
        self.house_prefs[key] = switch.get_active()
        house_map.save_prefs(self.house_prefs)
        if self.house_rec and self.house_rec.running and key == "minimap":
            self.set_minimap(switch.get_active())

    def set_minimap(self, on):
        """Shows the mini-map in the middle of your view (it carries the signal readout too), or, with it
        off, the plain floating readout."""
        if on:
            self.render_minimap()
        else:
            self.render_house_hud()
        self.set_readout("--minimap" if on else "--live")

    def render_minimap(self):
        rec = self.house_rec
        if not rec:
            return
        pose = rec.pose or rec._last or (0.0, house_map.EYE_HEIGHT, 0.0)
        dongle = self.house_target == "dongle"
        sel, oth = (rec.latest_dongle, rec.latest) if dongle else (rec.latest, rec.latest_dongle)
        dbm = sel.get("dbm") if sel else None
        word, colour = wifi_map.quality(dbm)
        reading = {"name": house_map.TARGETS[self.house_target], "dbm": dbm, "word": word, "colour": colour,
                   "other": f"{'Wi-Fi' if dongle else 'Dongle'} {dbm_text(oth)}"}
        if not rec.pose:
            reading["note"] = "Not tracking"
        try:
            svg = house_map.minimap_svg(house_map.add_walk(self.house_m, rec), pose, rec.heading,
                                        self.house_target, reading)
            loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
            loader.write(svg.encode())
            loader.close()
            write_rgba(loader.get_pixbuf(), self.MINIMAP)
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def on_house_clear(self):
        if self.house_rec and self.house_rec.running:
            self.stop_house_walk()
        house_map.clear()
        self.house_m = None
        self.house_floor = 0
        self._label_sel = self._label_undo = self._inspect = None
        self.house_view.update(zoom=1.0, pan=[0.0, 0.0])
        self.house_view.pop("anchor", None)
        set_info(self.house_progress, "Cleared. Press Start walking to map the house from scratch.")
        self.draw_house({})
        self.show_house_result()

    def label_name(self):
        return self.label_custom.get_text().strip() or self.house_label.value or "Spot"

    def on_label_custom(self, entry):
        if entry.get_text().strip():
            self.house_label.set_value(None)  # the typed name wins
            if self.label_mode.value in ("add", "rename"):
                self.label_status.set_text(f"Using your name \"{entry.get_text().strip()}\".")
        elif self.house_label.value is None:
            self.house_label.set_value("Router")

    def house_labels_map(self):
        """The saved map labels live in (made on the first label of a first walk)."""
        if self.house_m is None:
            self.house_m = {"kind": "house", "samples": [], "labels": [], "distance": 0.0, "walks": 0}
        return self.house_m

    def redraw_house_live(self):
        """The map as it is now: including the walk in progress."""
        rec = self.house_rec
        if rec and rec.running:
            self.draw_house(house_map.add_walk(self.house_m, rec))
            if self.house_prefs["minimap"]:
                self.render_minimap()
        else:
            self.draw_house(self.house_m or {})

    def on_label_here(self, *_):
        you = self.house_you()
        if not you:
            set_result(self.label_status, "Your position isn't known right now: wear the headset (and walk or "
                                          "keep this page open a moment).", False)
            return
        m = self.house_labels_map()
        samples = list(m.get("samples", [])) + (list(self.house_rec.samples) if self.house_rec else [])
        floor = house_map.floor_of(you[1], house_map.floor_base(samples)) if samples else 0
        name = self.label_name()
        self._label_undo = [dict(lab) for lab in m.setdefault("labels", [])]
        m["labels"].append({"name": name, "x": round(you[0], 2), "z": round(you[2], 2), "floor": floor})
        house_map.save(m)
        set_result(self.label_status, f"Added {name} where you're standing.", True)
        self.redraw_house_live()

    def on_house_tap(self, event):
        if not self._house_xf:
            return
        m = self.house_labels_map()
        xf = self._house_xf
        x, z = self.house_view_metres(xf, *self._map_xy(event))
        labels = m.setdefault("labels", [])
        near, best = None, 40 / xf["s"]  # a label within 40 px of the tap
        for i, lab in enumerate(labels):
            d = math.hypot(lab["x"] - x, lab["z"] - z)
            if lab.get("floor", 0) == self.house_floor and d <= best:
                near, best = i, d
        mode, name = self.label_mode.value, self.label_name()
        before = [dict(lab) for lab in labels]
        if mode == "add":
            labels.append({"name": name, "x": round(x, 2), "z": round(z, 2), "floor": self.house_floor})
            msg = f"Added {name}. Tap again to add another, or pick another name."
        elif mode == "move" and self._label_sel is not None and self._label_sel < len(labels):
            lab = labels[self._label_sel]
            lab["x"], lab["z"], lab["floor"] = round(x, 2), round(z, 2), self.house_floor
            self._label_sel = None
            msg = f"Moved {lab['name']}. Tap another label to move it."
        elif near is None:
            self.label_status.set_text("Tap right on a label's dot.")
            return
        elif mode == "move":
            self._label_sel = near
            self.label_status.set_text(f"Moving {labels[near]['name']}: now tap where it goes.")
            self.redraw_house_live()
            return
        elif mode == "rename":
            old = labels[near]["name"]
            labels[near]["name"] = name
            msg = f"Renamed {old} to {name}."
        else:
            msg = f"Deleted {labels.pop(near)['name']}."
        self._label_undo = before
        self.label_status.set_text(msg)
        house_map.save(m)
        self.redraw_house_live()
        if not (self.house_rec and self.house_rec.running):
            self.show_house_result()

    def on_label_mode(self, mode):
        self._label_sel = None
        if self.map_tool != "labels":
            self.set_map_tool("labels", redraw=False)
        self.label_status.set_text({
            "add": "Add: pick a name, then tap the map where it is.",
            "move": "Move: tap a label, then tap where it goes.",
            "rename": "Rename: pick the new name, then tap the label.",
            "delete": "Delete: tap the label to remove it."}[mode])
        if self.house_m:
            self.redraw_house_live()

    def on_label_name(self, name):
        if self.map_tool != "labels":
            self.set_map_tool("labels")
        if self.label_custom.get_text():
            self.label_custom.set_text("")  # a picked name replaces a typed one
        if self.label_mode.value == "add":
            self.label_status.set_text(f"Tap the map where the {name} is.")
        elif self.label_mode.value == "rename":
            self.label_status.set_text(f"Tap the label to rename it {name}.")

    def on_house_undo_label(self, *_):
        m = self.house_m
        if m and self._label_undo is not None:
            m["labels"], self._label_undo = self._label_undo, None
            self._label_sel = None
            self.label_status.set_text("Undone.")
            house_map.save(m)
            self.redraw_house_live()
            self.show_house_result()
        else:
            self.label_status.set_text("Nothing to undo.")

    # =========================================================== ceiling
    def build_ceiling_page(self):
        c = ceiling.load()
        box = add_class(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18), "page")
        head = Gtk.Box(spacing=18)
        back = Gtk.Button(label="‹ Back to rooms")
        back.connect("clicked", lambda *_: self.show_page("Rooms"))
        head.pack_start(back, False, False, 0)
        head.pack_start(label("Ceiling indicator", "page-title"), False, False, 0)
        box.pack_start(head, False, False, 0)

        show = card("Show")
        self.ceil_switches = {}
        rows = (("enabled", "Ceiling indicator", "Turns the whole thing on or off."),
                ("near", "Grid near hands or head", "A grid at your ceiling fades in when a controller or your "
                 "head gets within 40 cm of it."),
                ("always", "Always-on faint grid", "The same grid stays faintly visible all the time."),
                ("upmarker", "Real up marker", "While Recline tilts the world, markers point to your real "
                 "ceiling and floor."))
        for key, title, desc in rows:
            sw = Gtk.Switch()
            sw.set_active(bool(c.get(key)))
            sw.connect("notify::active", self.on_ceiling_switch, key)
            show.pack_start(switch_row(title, label(desc, "desc", wrap=True), sw), False, False, 0)
            self.ceil_switches[key] = sw

        hgt = card("Ceiling height", "Measure it once: hold a controller flat against the ceiling over your "
                                     "play area while the countdown runs (sitting or standing, not reclined).")
        self._ceil_height_timer = None
        self.ceil_height = Spin(150, 400, 1, int(round(c["height"] * 100)), width_chars=6,
                                fmt=lambda v: f"{v / 100:.2f} m", wrap=False,
                                on_change=lambda v: self.on_ceiling_height(v))
        hgt.pack_start(self.ceil_height, False, False, 0)
        self.ceil_measure_btn = add_class(Gtk.Button(label="Measure with a controller (5 s)"), "primary")
        self.ceil_measure_btn.connect("clicked", self.on_ceiling_measure)
        hgt.pack_start(self.ceil_measure_btn, False, False, 0)
        self.ceil_result = label("", "status", wrap=True)
        hgt.pack_start(self.ceil_result, False, False, 0)

        box.pack_start(self.two_columns(show, hgt), False, False, 0)
        self.ceil_status = label("", "desc", wrap=True)
        box.pack_start(self.ceil_status, False, False, 0)
        return box

    def show_ceiling_page(self):
        self.show_page("Ceiling")

    def refresh_ceiling_status(self):
        c = ceiling.load()
        on = bool(c["enabled"]) and ceiling.any_mode(c)
        height = ("Height not measured yet: using the value above." if not c.get("measured") else
                  f"Ceiling {c['height']:.2f} m above the floor.")

        def done(running):
            self.ceil_status.set_text(
                ("Indicator running. " if running is True else "Indicator off. " if not on else
                 "Indicator on, but not running right now (it runs while SteamVR does). ") + height)
        in_background(ceiling.service_active, done)

    def ceiling_save(self, c, message=None, centre=False):
        """Saves the settings (the overlay re-reads them by itself), then starts or stops the overlay to
        match, in order, in the background (systemd and SteamVR can take a moment).
        centre=True: also find the play area's middle first (where the grid goes)."""
        ceiling.save(c)
        restart = bool(c.get("upmarker") and c.get("enabled")) and self.render_ceiling_markers()

        def job():
            if centre:
                cx, cz = ceiling.play_area_centre()
                cur = ceiling.load()
                if (cur["cx"], cur["cz"]) == (0.0, 0.0):
                    cur["cx"], cur["cz"] = cx, cz
                    ceiling.save(cur)
            return ceiling.apply_service(ceiling.load(), restart=restart)

        def done(result):
            ok, msg = result if isinstance(result, tuple) else (False, str(result))
            if message:
                set_result(self.ceil_result, message if ok else f"{message} (but: {msg})", ok)
            elif not ok:
                set_result(self.ceil_result, f"Ceiling indicator: {msg}", False)
            self.refresh_ceiling_status()
        self.jobs.put(job, done)

    def on_ceiling_switch(self, sw, _pspec, key):
        if getattr(self, "_ceil_loading", False):
            return
        c = ceiling.load()
        c[key] = 1 if sw.get_active() else 0
        if key != "enabled" and sw.get_active() and not c["enabled"]:
            c["enabled"] = 1  # turning a mode on turns the indicator on
            self._ceil_loading = True
            self.ceil_switches["enabled"].set_active(True)
            self._ceil_loading = False
        self.ceiling_save(c, centre=bool(c["enabled"]) and (c["cx"], c["cz"]) == (0.0, 0.0))

    def on_ceiling_height(self, cm):
        """Saved after a short pause, so stepping through values isn't a save per tap (the overlay
        re-reads the file by itself)."""
        if self._ceil_height_timer:
            GLib.source_remove(self._ceil_height_timer)
        self._ceil_height_timer = GLib.timeout_add(700, self._save_ceiling_height, cm)

    def _save_ceiling_height(self, cm):
        self._ceil_height_timer = None
        c = ceiling.load()
        c["height"] = round(cm / 100, 2)
        ceiling.save(c)
        self.refresh_ceiling_status()
        return False

    def on_ceiling_measure(self, *_):
        self.ceil_measure_btn.set_sensitive(False)
        set_result(self.ceil_result, "Hold a controller flat against the ceiling now…", True)

        def done(result):
            self.ceil_measure_btn.set_sensitive(True)
            if isinstance(result, Exception):
                set_result(self.ceil_result, f"Couldn't measure: {result}", False)
                return
            (height, n), (cx, cz) = result
            if self._ceil_height_timer:  # a height typed meanwhile is replaced by the measured one
                GLib.source_remove(self._ceil_height_timer)
                self._ceil_height_timer = None
            c = ceiling.load()
            c.update(height=height, cx=cx, cz=cz, measured=1)
            self.ceil_height.set(int(round(height * 100)))
            self.ceiling_save(c, f"Ceiling: {height:.2f} m above the floor ({n} readings).")
        in_background(lambda: (ceiling.measure(), ceiling.play_area_centre()), done)

    def render_ceiling_markers(self):
        """Draws the overlay's UP / DOWN pictures when missing or out of date. Returns True when it drew
        new ones (a running overlay only loads them when it starts)."""
        stamp = hashlib.sha1((CSS.decode() + "\0" + updater.local_version()).encode()).hexdigest()
        stamp_file = os.path.join(ceiling.DATA, "stamp.txt")
        files = [os.path.join(ceiling.DATA, "up.rgba"), os.path.join(ceiling.DATA, "down.rgba")]
        if read(stamp_file) == stamp and all(os.path.exists(f) for f in files):
            return False
        os.makedirs(ceiling.DATA, exist_ok=True)
        try:
            with HudRenderer() as r:
                r.render("Real ceiling", "UP", files[0])
                r.render("Real floor", "DOWN", files[1])
            with open(stamp_file, "w", encoding="utf-8") as f:
                f.write(stamp)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return False
        return True

    def build_room_preview_page(self):
        """The big preview (no menu entry: opened from a room's Preview button)."""
        self.preview_box = add_class(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18), "page")
        return self.preview_box

    def show_room_preview(self, m, current=False):
        for child in self.preview_box.get_children():
            self.preview_box.remove(child)
        head = Gtk.Box(spacing=18)
        back = Gtk.Button(label="‹ Back to rooms")
        back.connect("clicked", lambda *_: self.show_page("Rooms"))
        head.pack_start(back, False, False, 0)
        title = label(("Room in use now" if current else m.get("name", "Room")), "page-title")
        title.set_ellipsize(Pango.EllipsizeMode.END)
        head.pack_start(title, True, True, 0)
        self.preview_box.pack_start(head, False, False, 0)

        row = Gtk.Box(spacing=26)
        img = Gtk.Image()
        try:
            img.set_from_pixbuf(self.room_preview(m, w=1000, h=760, grid=True))
        except GLib.Error:
            pass
        row.pack_start(img, False, False, 0)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        facts = []
        if not current and m.get("saved"):
            facts.append("Saved " + time.strftime("%a %b %d, %I:%M %p", time.localtime(m["saved"])))
        if m.get("outline"):
            xs, zs = [p[0] for p in m["outline"]], [p[1] for p in m["outline"]]
            facts += [f"Size: about {max(xs) - min(xs):.1f} × {max(zs) - min(zs):.1f} m",
                      f"Floor inside the boundary: {m.get('area', 0)} m²",
                      f"Boundary points: {m.get('walls', 0)}"]
        facts.append(f"Room map: {m.get('map_mb', 0)} MB")
        side.pack_start(label("\n".join(facts), "status", wrap=True), False, False, 0)
        side.pack_start(label("White dot: where you stand when the room starts.\nWhite line: the way you face.\n"
                              "Blue: the area inside your boundary.", "desc", wrap=True), False, False, 0)
        if not current:
            side.pack_start(ConfirmButton("Load this room", "Tap again: restarts VR",
                                          lambda: self.load_room(m["id"], m.get("name")), css="primary"),
                            False, False, 0)
        row.pack_start(side, True, True, 0)
        self.preview_box.pack_start(row, False, False, 0)
        self.preview_box.show_all()
        self.stack.set_visible_child_name("RoomPreview")

    def refresh_rooms(self):
        """Reading the room in use needs the system journal (slow after a long uptime): gather
        everything in the background, then build the page."""
        self._rooms_seq += 1
        seq = self._rooms_seq

        def gather():
            return rooms.current(), rooms.list_rooms(), rooms.backup_exists()

        def done(result):
            if seq != self._rooms_seq:
                return  # a newer refresh is on its way
            if isinstance(result, Exception):
                set_result(self.rooms_result, f"Couldn't read the rooms: {result}", False)
                return
            self.fill_rooms(*result)
        in_background(gather, done)

    def fill_rooms(self, now, saved, backup):
        def facts(m):
            if not m.get("maps"):
                return "No room map yet."
            return f"{m.get('area', 0)} m² boundary · map {m.get('map_mb', 0)} MB"
        try:
            self.room_now_img.set_from_pixbuf(self.room_preview(now))
        except GLib.Error:
            pass
        self.room_now_lbl.set_text(facts(now))
        self.undo_btn.set_sensitive(backup)

        for child in self.rooms_grid_holder.get_children():
            self.rooms_grid_holder.remove(child)
        cards_ = []
        for m in saved:
            c = card()
            row = Gtk.Box(spacing=18)
            img = Gtk.Image()
            try:
                img.set_from_pixbuf(self.room_preview(m))
            except GLib.Error:
                pass
            row.pack_start(img, False, False, 0)
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            col.pack_start(label(m.get("name", "Room"), "card-title", wrap=True), False, False, 0)
            when = time.strftime("%b %d, %I:%M %p", time.localtime(m.get("saved", 0)))
            col.pack_start(label(f"Saved {when}\n{facts(m)}", "desc", wrap=True), False, False, 0)
            btns = Gtk.Box(spacing=10, homogeneous=True)
            prev = Gtk.Button(label="Preview")
            prev.connect("clicked", lambda _b, room=m: self.show_room_preview(room))
            btns.pack_start(prev, True, True, 0)
            btns.pack_start(ConfirmButton("Load", "Tap again: restarts VR",
                                          lambda rid=m["id"], n=m.get("name"): self.load_room(rid, n),
                                          css="primary"), True, True, 0)
            col.pack_start(btns, False, False, 0)
            row.pack_start(col, True, True, 0)
            c.pack_start(row, False, False, 0)

            # rename + delete
            edit = Gtk.Box(spacing=10)
            entry = Gtk.Entry(text=m.get("name", ""), max_length=40)
            edit.pack_start(entry, True, True, 0)
            ren = Gtk.Button(label="Rename")
            ren.connect("clicked", lambda _b, rid=m["id"], e=entry: self.rename_room(rid, e.get_text()))
            edit.pack_start(ren, False, False, 0)
            edit.pack_start(ConfirmButton("Delete", "Tap again", lambda rid=m["id"], n=m.get("name"):
                                          self.delete_room(rid, n), css="primary"), False, False, 0)
            c.pack_start(edit, False, False, 0)
            cards_.append(c)
        if cards_:
            grid = self.two_columns(*cards_)
            grid.set_hexpand(True)
            self.rooms_grid_holder.pack_start(grid, True, True, 0)
        else:
            self.rooms_grid_holder.pack_start(label("None yet. Save the room you're in above.", "desc"),
                                              False, False, 0)
        self.rooms_grid_holder.show_all()

    def reset_save_button(self):
        if self._replace_timer:
            GLib.source_remove(self._replace_timer)
            self._replace_timer = None
        self._replace_armed = (None, 0)
        self.room_save_lbl.set_text("Save")
        self.room_save_btn.get_style_context().remove_class("armed")

    def _replace_expired(self):
        self._replace_timer = None  # ends by returning False
        self.reset_save_button()
        return False

    def on_save_room(self, *_):
        name = " ".join(self.room_name.get_text().split())[:40] or "Room"
        existing = rooms.find_by_name(name)
        armed_name, until = self._replace_armed
        if existing and not (armed_name == name and time.monotonic() < until):
            self.reset_save_button()
            self._replace_armed = (name, time.monotonic() + 5)
            self.room_save_lbl.set_text(f"Tap again to replace '{name}'")
            self.room_save_btn.get_style_context().add_class("armed")
            self._replace_timer = GLib.timeout_add(5000, self._replace_expired)
            return
        self.reset_save_button()
        self.room_save_btn.set_sensitive(False)  # one save at a time (copying the room map takes a moment)
        set_info(self.rooms_result, f"Saving '{name}'…")

        def done(result):
            self.room_save_btn.set_sensitive(True)
            if isinstance(result, Exception):
                set_result(self.rooms_result, f"Couldn't save: {result}", False)
            else:
                set_result(self.rooms_result, f"Saved this room as '{name}'.", True)
            self.refresh_rooms()
        in_background(lambda: rooms.save(name, existing), done)

    def rename_room(self, rid, name):
        name = " ".join(name.split())[:40]
        if not name:
            set_result(self.rooms_result, "Type a name first.", False)
            return

        def done(result):
            if isinstance(result, Exception):
                set_result(self.rooms_result, f"Couldn't rename: {result}", False)
            else:
                set_result(self.rooms_result, f"Renamed to '{name}'.", True)
            self.refresh_rooms()
        in_background(lambda: rooms.rename(rid, name), done)

    def delete_room(self, rid, name):
        def done(result):
            if isinstance(result, Exception):
                set_result(self.rooms_result, f"Couldn't delete: {result}", False)
            else:
                set_result(self.rooms_result, f"Deleted '{name}'.", True)
            self.refresh_rooms()
        in_background(lambda: rooms.delete(rid), done)

    def load_room(self, rid, name):
        def done(result):
            ok, msg = result if isinstance(result, tuple) else (False, str(result))
            set_result(self.rooms_result, f"Loading '{name}': SteamVR restarts now. Reopen Deckhand in a moment."
                       if ok else f"Couldn't load: {msg}", ok)
        in_background(lambda: rooms.start_load(rid), done)

    # ============================================================ recline
    def build_recline(self):
        box = self.page("Recline")
        c = card(None, "Recline VR lets you play lying down. Lie back and use SteamVR's recenter (it reclines "
                       "once you're tilted past 35°), or use the buttons below.")
        self.recline_lbl = label("", "status", wrap=True)
        c.pack_start(self.recline_lbl, False, False, 0)
        row = Gtk.Box(spacing=18, homogeneous=True)
        self.recline_btns = []
        for text, args in (("Recline to where I'm looking", ["recenter"]), ("Back to normal", ["reset"])):
            b = add_class(Gtk.Button(label=text), "tile")
            b.connect("clicked", self.on_recline, args, text)
            row.pack_start(b, True, True, 0)
            self.recline_btns.append(b)
        c.pack_start(row, False, False, 0)
        self.recline_result = label("", "status")
        c.pack_start(self.recline_result, False, False, 0)
        c.pack_start(label("In any game: hold both grips and click both thumbsticks for Recline's own panel "
                           "(tilt angle, eye height).", "desc", wrap=True), False, False, 0)
        box.pack_start(c, False, False, 0)
        return box

    def refresh_recline(self):
        avail = recline_available()
        for b in self.recline_btns:
            b.set_sensitive(avail)
        self.recline_lbl.set_text("Checking…")
        in_background(recline_status, lambda s: self.recline_lbl.set_text(str(s)))

    def on_recline(self, _btn, args, text):
        def done(result):
            rc, out = result if isinstance(result, tuple) else (1, str(result))
            set_result(self.recline_result, f"{text}: done" if rc == 0 else f"Failed: {out[-200:]}", rc == 0)
            self.refresh_recline()
        in_background(lambda: run([RECLINECTL, *args], env=vr_tool_env()), done)

    # ============================================================= system
    def build_system(self):
        box = self.page("System")
        info = card("This headset")
        self.sys_lbl = label("", "status", selectable=True, wrap=True)
        info.pack_start(self.sys_lbl, False, False, 0)
        restart = card("Restart SteamVR", "Fixes most glitches. Closes everything that is open, including "
                                          "this app; VR comes back in a few seconds.")
        restart.pack_start(ConfirmButton("Restart SteamVR", "Tap again to restart", self.do_restart_steamvr,
                                         css="primary"), False, False, 0)
        box.pack_start(self.two_columns(info, restart), False, False, 0)

        upd = card("Update app", "Gets the newest Deckhand from GitHub. Your settings, rooms, maps and "
                                 "timers stay as they are.")
        self.started_version = updater.local_version()  # what this window is running
        self.upd_lbl = label(f"Installed: version {self.started_version}", "status", wrap=True)
        upd.pack_start(self.upd_lbl, False, False, 0)
        ubox = Gtk.Box(spacing=10, homogeneous=True)
        self.upd_btn = Gtk.Button(label="Check for update")
        self.upd_btn.connect("clicked", self.on_update_check)
        ubox.pack_start(self.upd_btn, True, True, 0)
        self.upd_go = add_class(Gtk.Button(label="Update"), "primary")
        self.upd_go.connect("clicked", self.on_update_install)
        self.upd_go.set_sensitive(False)  # greyed out until a newer version is found
        ubox.pack_start(self.upd_go, True, True, 0)
        upd.pack_start(ubox, False, False, 0)
        self.upd_restart = add_class(Gtk.Button(label="Restart Deckhand to use the new version"), "primary")
        self.upd_restart.connect("clicked", lambda *_: self.restart_self())
        self.upd_restart.set_no_show_all(True)  # shown once a new version is installed
        upd.pack_start(self.upd_restart, False, False, 0)
        self.upd_new = None
        self.upd_checked = False
        share = card("Share with a friend", "On their Steam Frame, in Desktop mode, open Konsole, type this "
                                            "and press Enter:")
        self.share_cmd = updater.INSTALL_SHORT
        share.pack_start(label(self.share_cmd, "big-cmd", selectable=True), False, False, 0)
        self.share_copy = add_class(Gtk.Button(label="Copy to clipboard"), "primary")
        self.share_copy.connect("clicked", self.on_share_copy)
        share.pack_start(self.share_copy, False, False, 0)
        share.pack_start(label(f"Same as the full command on github.com/{updater.REPO}", "desc", wrap=True,
                               selectable=True), False, False, 0)

        rm = card("Remove Deckhand", "Uninstalls the app and everything it installed on this Frame.")
        self.rm_open = Gtk.Button(label="Remove Deckhand…")
        self.rm_open.connect("clicked", lambda *_: (self.rm_box.set_no_show_all(False), self.rm_box.show_all(),
                                                    self.rm_open.hide(), self.refresh_remove_warning()))
        rm.pack_start(self.rm_open, False, False, 0)
        self.rm_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.rm_box.set_no_show_all(True)
        self.rm_warn = label("", "status", wrap=True, markup=True)
        self.rm_box.pack_start(self.rm_warn, False, False, 0)
        self.rm_keep = Gtk.Switch(active=False)
        self.rm_keep.connect("notify::active", lambda *_: self.refresh_remove_warning())
        self.rm_box.pack_start(switch_row("Keep my data", label("Keep my saved rooms, maps and reminders",
                                                                "desc", wrap=True), self.rm_keep), False, False, 0)
        rrow = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)  # stacked: fits in half a row
        rrow.pack_start(HoldButton("Hold 5 s to remove everything", self.do_remove_all, seconds=5.0),
                        False, False, 0)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: (self.rm_box.hide(), self.rm_open.show()))
        rrow.pack_start(cancel, False, False, 0)
        self.rm_box.pack_start(rrow, False, False, 0)
        rm.pack_start(self.rm_box, False, False, 0)
        box.pack_start(self.two_columns(upd, rm), False, False, 0)
        # The install command gets a whole row: it's big and must stay on one line (half a row made
        # the window wider than the screen).
        box.pack_start(share, False, False, 0)
        box.pack_start(label(f"Deckhand lives in {ROOT}", "desc", wrap=True), False, False, 0)
        return box

    def on_share_copy(self, *_):
        """Puts the install command on the clipboard (kept after Deckhand closes where a clipboard
        manager runs, e.g. Desktop mode)."""
        clip = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clip.set_text(self.share_cmd, -1)
        clip.store()
        self.share_copy.set_label("Copied ✓")
        GLib.timeout_add(3000, lambda: (self.share_copy.set_label("Copy to clipboard"), False)[1])

    def on_update_check(self, *_):
        self.upd_checked = True
        self.upd_btn.set_sensitive(False)
        self.upd_lbl.set_text("Checking GitHub…")

        def checked(res):
            self.upd_btn.set_sensitive(True)
            if isinstance(res, Exception):
                set_result(self.upd_lbl, f"Couldn't reach GitHub ({res}). Is the Frame online?", False)
                return
            here, there, newer = res
            if newer:
                self.upd_new = there
                set_result(self.upd_lbl, f"Version {there} is out (you have {here}).", True)
                self.upd_go.set_label(f"Update to {there}")
                self.upd_go.set_sensitive(True)
            else:
                self.upd_new = None
                self.upd_go.set_label("Update")
                self.upd_go.set_sensitive(False)
                set_result(self.upd_lbl, f"Up to date: version {here}.", True)
            self.show_restart_if_needed()
        in_background(updater.check, checked)

    def show_restart_if_needed(self):
        """A newer version is installed than this window is running (updated, or files replaced)."""
        installed = updater.local_version()
        if installed != self.started_version:
            set_result(self.upd_lbl, f"Version {installed} is installed. Restart Deckhand to use it "
                                     f"(this window is still {self.started_version}).", True)
            self.upd_restart.set_no_show_all(False)
            self.upd_restart.show()

    def on_update_install(self, *_):
        if not self.upd_new:
            return
        self.upd_btn.set_sensitive(False)
        self.upd_go.set_sensitive(False)
        self.upd_lbl.set_text(f"Updating to version {self.upd_new}…")

        def done(res):
            self.upd_btn.set_sensitive(True)
            if isinstance(res, Exception):
                self.upd_go.set_sensitive(True)
                msg = str(res).strip()
                set_result(self.upd_lbl, (msg if msg.startswith("Updated files") else f"Update failed: {msg}")
                           + " Tap Update to try again.", False)
                return
            self.upd_new = None
            self.upd_go.set_label("Update")
            self.upd_go.set_sensitive(False)
            self.show_restart_if_needed()
        in_background(lambda: updater.install(log=lambda _m: None), done)

    def refresh_remove_warning(self):
        keep = self.rm_keep.get_active()
        old_helper = 0 < helper_version() < 4
        lines = [
            "<b>Warning: this removes everything Deckhand installed on this Frame.</b>",
            "• The app, its Steam library entry and its app-menu entry",
            "• Its background jobs: auto shut-off, sleep timer, ceiling indicator, reminders, kitchen timer",
            "• The fan helper and its password-free rule, if you ran setup: the fan goes back to stock",
            "• Screenshots are turned back on (if you blocked them here)",
            ("• <b>Kept:</b> your saved rooms, Wi-Fi and house maps and reminders" if keep else
             "• <b>Also deleted:</b> your saved rooms, Wi-Fi and house maps, reminders and old exports "
             "(~/Documents/Deckhand). This can't be undone."),
            "Not touched: SteamVR, its other settings, and the room/boundary SteamVR is using now.",
        ]
        if old_helper:
            lines.append("Your fan helper is from an older setup: afterwards, two small system files need your "
                         "password to remove. Run in Konsole: <tt>sudo rm -f /etc/sudoers.d/zz-frame-settings "
                         "&amp;&amp; sudo rm -rf /etc/frame-settings</tt>")
        self.rm_warn.set_markup("\n".join(lines))

    def do_remove_all(self):
        """Runs uninstall.sh as its own job (it outlives this window), then closes the app."""
        if getattr(self, "wifi_rec", None):
            self.stop_wifi_mapping()
        if getattr(self, "house_rec", None):
            self.stop_house_walk()
        if not vr_get("userinterface", "screenshots", "bool", True):
            vr_set("userinterface", "screenshots", "bool", True)
        args = ["systemd-run", "--user", "--quiet", "--collect", "--unit=deckhand-uninstall",
                "/bin/bash", os.path.join(ROOT, "uninstall.sh")]
        if self.rm_keep.get_active():
            args.append("--keep-data")
        r = subprocess.run(args, capture_output=True, text=True)
        if r.returncode != 0:
            set_result(self.rm_warn, f"Couldn't start the removal: {(r.stderr or r.stdout).strip()}", False)
            return
        set_result(self.rm_warn, "Removing Deckhand… this window closes now. Goodbye!", True)
        GLib.timeout_add(1200, lambda: (Gtk.main_quit(), False)[1])

    def restart_self(self):
        """Starts the new version in place of this one (same process, so Steam still counts it as running)."""
        if getattr(self, "wifi_rec", None):
            self.stop_wifi_mapping()
        if getattr(self, "house_rec", None):
            self.stop_house_walk()
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            os.execv(sys.executable, [sys.executable, os.path.join(HERE, "deckhand.py")])
        except OSError as e:
            set_result(self.upd_lbl, f"Couldn't restart ({e}). Close Deckhand and open it again.", False)
        return False

    @staticmethod
    def colour_camera():
        """True/False: is the colour passthrough camera accessory connected (its kernel driver says
        present=1)? None if this headset doesn't report it."""
        try:
            with open("/sys/bus/i2c/drivers/arcimx616/0-001a/led_status", encoding="utf-8") as f:
                m = re.search(r"present=(\d)", f.read())
        except OSError:
            return None
        return (m.group(1) == "1") if m else None

    def refresh_system(self):
        pct, chg = battery()
        lines = [f"Battery: {pct}%" + (" (charging)" if chg else "") if pct is not None else "Battery: unknown",
                 f"Name: {socket.gethostname()}",
                 "Network:\n  " + "\n  ".join(ip_addresses()),
                 "SteamVR: " + ("running" if steamvr_running() else "not running")]
        cam = self.colour_camera()
        if cam is not None:
            lines.append("Colour passthrough camera: " + ("connected" if cam else
                         "NOT DETECTED: take it off, reattach it firmly, then restart SteamVR"))
        self.sys_lbl.set_text("\n".join(lines))

    def do_restart_steamvr(self):
        # --no-block: systemd does the restart by itself; this returns at once (this window closes too)
        subprocess.run(["systemctl", "--user", "restart", "--no-block", "steamvr.service"], capture_output=True,
                       timeout=15)


def snapshot(prefix):
    """Render every page to PNGs as a real full-screen window.

    Only on a virtual display, so nothing ever pops up in the headset, e.g.:
      GDK_DPI_SCALE=1.5 xvfb-run -a -s "-screen 0 1920x1080x24" python3 app/deckhand.py --snapshot /tmp/x
    (GDK_DPI_SCALE=1.5 matches the headset's 144 dpi text scaling.)
    """
    if os.environ.get("DISPLAY") in (":0", ":1"):
        raise SystemExit("snapshot only runs on a virtual display (xvfb-run), never on the headset's")
    win = FrameSettings()
    win.show_all()
    win.show_setup(not win.setup_ready)
    win.fullscreen()
    print("window minimum width:", win.get_preferred_width()[0],
          "| screen:", Gdk.Screen.get_default().get_width())
    shots = [(name, lambda name=name: win.show_page(name)) for name, _icon in win.PAGES] + [
        ("WifiMap", win.show_wifi_page),
        ("House", win.show_house_page),
        ("House-dongle", lambda: (win.house_target_seg.set_value("dongle"), win.on_house_target("dongle"))),
        ("Ceiling", win.show_ceiling_page)]
    if win.house_m and win.house_m.get("samples"):  # the mini-map, as it would look in the middle of a walk
        smp = win.house_m["samples"][len(win.house_m["samples"]) // 2]
        loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
        word, colour = wifi_map.quality(-61)
        loader.write(house_map.minimap_svg(win.house_m, (smp[0], smp[1], smp[2]), (0.6, -0.8), "wifi",
                                           {"name": "Home Wi-Fi", "dbm": -61, "word": word, "colour": colour,
                                            "other": "Dongle -53"}).encode())
        loader.close()
        loader.get_pixbuf().savev(f"{prefix}-minimap.png", "png", [], [])
        print(f"wrote {prefix}-minimap.png")
    for n, (name, show) in enumerate(shots):
        show()
        for _ in range(150):  # let background lookups (e.g. Recline status) and layout finish
            while Gtk.events_pending():
                Gtk.main_iteration_do(False)
            time.sleep(0.01)
        gw = win.get_window()
        pix = Gdk.pixbuf_get_from_window(gw, 0, 0, gw.get_width(), gw.get_height())
        pix.savev(f"{prefix}-{n}.png", "png", [], [])
        print(f"wrote {prefix}-{n}.png {name} ({pix.get_width()}x{pix.get_height()})")


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--snapshot":
        snapshot(sys.argv[2])
        return
    win = FrameSettings()
    win.show_all()
    win.show_setup(not win.setup_ready)
    if os.environ.get("SteamGamepadUI") or os.environ.get("SteamTenfoot"):
        win.fullscreen()  # in VR: fill the whole screen
    else:
        win.maximize()    # desktop mode
    Gtk.main()


if __name__ == "__main__":
    main()
