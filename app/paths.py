"""Where Deckhand's parts live, and how its helper programs are started.
The app works from any install folder (~/Deckhand for new installs).

    <root>/app/       the Python app (this folder)
    <root>/helpers/   native SteamVR helpers: sources in src/, built into bin/ by build.sh
    <root>/assets/    icon and Steam library artwork
    <root>/logs/      run logs
"""
import os

APP = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(APP)
BIN = os.path.join(ROOT, "helpers", "bin")
ASSETS = os.path.join(ROOT, "assets")
LOGS = os.path.join(ROOT, "logs")
ICON = os.path.join(ASSETS, "steam", "icon.png")


def tool(name):
    """Full path of a built native helper (timer_overlay, space_tool, ceiling_overlay)."""
    return os.path.join(BIN, name)


def clean_env():
    """Environment for SteamVR tools (vrcmd, reclinectl, space_tool, ...).

    Started from Steam, Deckhand carries SteamAppId/SteamGameId; a SteamVR client that inherits
    them connects *as this app* and SteamVR then reloads the app's controller input on every
    call, which froze the window. A plain environment makes them ordinary background tools."""
    keep = ("HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
    return {k: os.environ[k] for k in keep if k in os.environ}


def runtime_dir():
    """A RAM-backed folder for files rewritten every second (in-view readouts): no flash wear.
    Falls back to ~/.cache when XDG_RUNTIME_DIR isn't set."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and os.path.isdir(base):
        return os.path.join(base, "deckhand")
    return os.path.expanduser("~/.cache/frame-settings")
