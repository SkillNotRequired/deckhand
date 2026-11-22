#!/bin/bash
# Removes Deckhand and everything it set up on this Steam Frame (System tab > Remove Deckhand runs
# this; you can also run it from Konsole):
#
#   ~/Deckhand/uninstall.sh              remove everything, including your saved rooms, maps,
#                                        reminders and old exports (~/Documents/Deckhand)
#   ~/Deckhand/uninstall.sh --keep-data  keep those (settings and data folders, ~/Documents/Deckhand)
#   DECKHAND_DRY_RUN=1 ~/Deckhand/uninstall.sh   only print what it would do
#
# Removes: its background services (auto shut-off, sleep timer, ceiling indicator, reminders,
# kitchen timer, live readouts), the fan helper and its password-free sudo rule (fan back to stock),
# the Steam library shortcut and app-menu entry, and this folder. SteamVR itself, its settings and
# the room/boundary SteamVR is using are not touched. A log is left in /tmp/deckhand-uninstall.log.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
KEEP=0
[ "${1:-}" = "--keep-data" ] && KEEP=1
DRY=${DECKHAND_DRY_RUN:-}
act() { if [ -n "$DRY" ]; then echo "would run: $*"; else "$@"; fi; }
[ -n "$DRY" ] || exec > >(tee /tmp/deckhand-uninstall.log) 2>&1
echo "Removing Deckhand from $HERE  $(date -Is)  keep-data=$KEEP${DRY:+  (dry run)}"
[ -n "$DRY" ] || sleep 2  # give the app a moment to close

# Background services
UNITS=~/.config/systemd/user
for unit in frame-autooff.service frame-ceiling.service frame-kitchen-timer.service frame-room-load.service \
            frame-wifi-live.service frame-minimap.service \
            $(cd "$UNITS" 2>/dev/null && ls frame-reminder-*.timer frame-reminder-*.service 2>/dev/null); do
  if systemctl --user cat "$unit" >/dev/null 2>&1 || systemctl --user is-active --quiet "$unit"; then
    act systemctl --user disable --now "$unit"
    act systemctl --user stop "$unit"
  fi
done
for f in "$UNITS"/frame-autooff.service "$UNITS"/frame-ceiling.service \
         "$UNITS"/frame-reminder-*.service "$UNITS"/frame-reminder-*.timer; do
  [ -e "$f" ] && act rm -f "$f"
done
act systemctl --user daemon-reload

# Fan helper (root). Version 4+ removes itself and its sudo rule; older ones can only restore the fan.
LEFT=0
if [ -e /etc/frame-settings/fanmode ]; then
  VER=$(sudo -n /etc/frame-settings/fanmode check 2>/dev/null | awk '{print $2}')
  if [ "${VER:-0}" -ge 4 ] 2>/dev/null; then
    act sudo -n /etc/frame-settings/fanmode uninstall
  else
    act sudo -n /etc/frame-settings/fanmode off
    LEFT=1
  fi
fi

# Steam library shortcut, app-menu entry, icon
act env -u SteamAppId -u SteamGameId -u LD_PRELOAD python3 "$HERE/app/steam_shortcut.py" remove \
  || echo "(Steam shortcut: remove it from the library by hand if it's still there)"

# Data
act rm -rf ~/.cache/frame-settings
if [ "$KEEP" = 0 ]; then
  act rm -rf ~/.config/frame-settings ~/.local/share/frame-settings ~/Documents/Deckhand
else
  echo "keeping: ~/.config/frame-settings ~/.local/share/frame-settings ~/Documents/Deckhand"
fi

# The app itself (only a folder that really is Deckhand)
if [ -f "$HERE/app/deckhand.py" ] && [ -f "$HERE/app/updater.py" ] && [ "$HERE" != "$HOME" ] && [ "$HERE" != "/" ]; then
  act rm -rf "$HERE"
fi

if [ "$LEFT" = 1 ]; then
  echo
  echo "Two system files from an older fan setup need your password to remove. In Konsole run:"
  echo "  sudo rm -f /etc/sudoers.d/zz-frame-settings && sudo rm -rf /etc/frame-settings"
fi
echo "Deckhand removed.${DRY:+ (dry run: nothing was changed)}"
