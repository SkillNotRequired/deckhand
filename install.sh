#!/bin/bash
# Installs (or updates) Deckhand on a Steam Frame. In Desktop mode, open Konsole and run:
#
#   curl -fsSL https://is.gd/deckhandvr|sh          (short link to this file)
#   curl -fsSL https://raw.githubusercontent.com/SkillNotRequired/deckhand/main/install.sh | bash
#
# Puts the app in ~/Deckhand (or $DECKHAND_DIR), builds its small helpers, and adds it to
# the Steam library (with artwork) and the app menu. Nothing outside your home folder is
# touched. Fan control needs one more step with sudo; the end of this script says how.
#
# Everything happens in main(), called on the last line: if the download is cut short,
# the shell never gets there and nothing runs.
set -euo pipefail

REPO="SkillNotRequired/deckhand"

say() { printf '\n\033[1;36m%s\033[0m\n' "$*"; }

main() {
  if [ "$(id -u)" = 0 ]; then
    echo "Run this as your normal user, without sudo: Deckhand installs into your home folder." >&2
    exit 1
  fi

  DEST="${DECKHAND_DIR:-$HOME/Deckhand}"
  # An earlier install under the old name stays where it is (its services point there).
  if [ -z "${DECKHAND_DIR:-}" ] && [ ! -d "$DEST" ] && { [ -f "$HOME/FrameSettings/frame_settings.py" ] ||
       [ -f "$HOME/FrameSettings/app/deckhand.py" ]; }; then
    DEST="$HOME/FrameSettings"
  fi
  # Never copy into a folder that holds something else.
  if [ -d "$DEST" ] && [ -n "$(ls -A "$DEST")" ] && [ ! -f "$DEST/app/deckhand.py" ] &&
     [ ! -f "$DEST/frame_settings.py" ]; then
    echo "$DEST already exists and isn't a Deckhand folder; nothing installed." >&2
    echo "Move it, or install somewhere else: curl -fsSL https://is.gd/deckhandvr | DECKHAND_DIR=~/Deckhand2 sh" >&2
    exit 1
  fi

  [ "$(uname -m)" = aarch64 ] || echo "Note: this is made for the Steam Frame (aarch64); this machine is $(uname -m)."
  [ -d /opt/steamvr ] || { echo "SteamVR isn't installed in /opt/steamvr. Is this a Steam Frame?" >&2; exit 1; }

  say "Downloading Deckhand…"
  TMP=$(mktemp -d)
  trap 'rm -rf "$TMP"' EXIT
  curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/heads/main" | tar -xz -C "$TMP"
  SRC=$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -1)
  [ -f "$SRC/app/deckhand.py" ] || { echo "Download looks wrong; nothing installed." >&2; exit 1; }

  say "Installing to $DEST"
  mkdir -p "$DEST"
  cp -a "$SRC/." "$DEST/"
  chmod +x "$DEST"/*.sh "$DEST"/helpers/*.sh "$DEST/app/deckhand.py"
  python3 "$DEST/app/updater.py" cleanup   # tidy an older install's flat layout

  say "Building helpers (a few seconds)…"
  if ! bash "$DEST/helpers/build.sh"; then
    echo "Warning: the helpers didn't build. Timers, maps and the ceiling indicator need them;" >&2
    echo "run $DEST/helpers/build.sh later. Everything else works." >&2
  fi
  [ -n "${DECKHAND_TEST:-}" ] || python3 "$DEST/app/updater.py" migrate >/dev/null 2>&1 || true   # services follow the app

  if [ -z "${DECKHAND_TEST:-}" ]; then  # DECKHAND_TEST=1: test install, leaves Steam and services alone
    say "Adding it to Steam and the app menu…"
    if ! python3 "$DEST/app/steam_shortcut.py"; then
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
}

main "$@"
