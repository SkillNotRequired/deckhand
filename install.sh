#!/bin/bash
# Installs (or updates) Deckhand on a Steam Frame. In Desktop mode, open Konsole and run:
#
#   curl -fsSL https://raw.githubusercontent.com/SkillNotRequired/deckhand/main/install.sh | bash
#
# Puts the app in ~/Deckhand (or $DECKHAND_DIR), builds its small helpers, and adds it to
# the Steam library (with artwork) and the app menu. Nothing outside your home folder is
# touched. Fan control needs one more step with sudo; the end of this script says how.
set -euo pipefail

REPO="SkillNotRequired/deckhand"
DEST="${DECKHAND_DIR:-$HOME/Deckhand}"
# An earlier install under the old name stays where it is (its services point there).
[ -z "${DECKHAND_DIR:-}" ] && [ ! -d "$DEST" ] && [ -f "$HOME/FrameSettings/frame_settings.py" ] && DEST="$HOME/FrameSettings"

say() { printf '\n\033[1;36m%s\033[0m\n' "$*"; }

[ "$(uname -m)" = aarch64 ] || echo "Note: this is made for the Steam Frame (aarch64); this machine is $(uname -m)."
[ -d /opt/steamvr ] || { echo "SteamVR isn't installed in /opt/steamvr. Is this a Steam Frame?" >&2; exit 1; }

say "Downloading Deckhand…"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/heads/main" | tar -xz -C "$TMP"
SRC=$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -1)
[ -f "$SRC/frame_settings.py" ] || { echo "Download looks wrong; nothing installed." >&2; exit 1; }

say "Installing to $DEST"
mkdir -p "$DEST"
cp -a "$SRC/." "$DEST/"
chmod +x "$DEST"/*.sh "$DEST/frame_settings.py"

say "Building helpers (a few seconds)…"
bash "$DEST/build.sh"

if [ -z "${DECKHAND_SKIP_SHORTCUT:-}" ]; then  # (set it to test an install without touching Steam)
  say "Adding it to Steam and the app menu…"
  if ! python3 "$DEST/steam_shortcut.py"; then
    echo "(The app menu entry is in. For the Steam library, add $DEST/launch.sh as a Non-Steam Game.)"
  fi
fi

say "Done: Deckhand $(cat "$DEST/VERSION")"
cat <<EOF
Open it from your Steam library (works in VR) or the app menu.

Optional, for fan control: run this once and type your password
(no password yet? set one first with: passwd)

    sudo $DEST/setup.sh

Later updates: System tab > Update app.
EOF
