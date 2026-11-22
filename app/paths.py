"""Where Deckhand's parts live. The app works from any install folder (~/Deckhand for new installs).

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
    """Full path of a built native helper (timer_overlay, space_tool, chaperone_tool, ceiling_overlay)."""
    return os.path.join(BIN, name)
