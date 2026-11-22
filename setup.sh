#!/bin/bash
# One-time setup for Deckhand. Run in a terminal on the Frame:
#
#   sudo ~/Deckhand/setup.sh            install   (use the folder Deckhand is in)
#   sudo ~/Deckhand/setup.sh remove     undo (also puts the fan back to stock)
#
# Installs the fan helper as a root-owned file (/etc/frame-settings/fanmode) and
# a sudoers rule that lets your user run exactly that helper without a password,
# so the app can change the fan mode from inside VR. Nothing else gets password-free
# access, and the helper only accepts "on <floor> [<floor>] [<strength>]", "off",
# "uninstall", "poweroff" or "check". Screenshots need no setup.
#
# sudo applies the LAST matching rule, and reads /etc/sudoers.d in name order,
# so the rule file is named zz-* to come after SteamOS's own wheel rule.
#
# Runs as root: a fixed PATH and locale, so nothing from the calling shell decides
# which programs run.
PATH=/usr/bin:/usr/sbin:/bin:/sbin; export PATH; umask 022; export LC_ALL=C
set -u

[ "$(id -u)" = 0 ] || { echo "run with: sudo $0 ${1:-}" >&2; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)
TARGET_USER=${SUDO_USER:-}
[ -n "$TARGET_USER" ] && [ "$TARGET_USER" != root ] || { echo "run it with sudo from your own user" >&2; exit 1; }
HELPER_DIR=/etc/frame-settings
HELPER=$HELPER_DIR/fanmode
SUDOERS=/etc/sudoers.d/zz-frame-settings
OLD_SUDOERS=/etc/sudoers.d/frame-settings
LOG=$HERE/logs/setup.log

# Everything below also goes to setup.log (readable without root) for troubleshooting.
# The folder is the user's (they can write in it), so root never writes there itself: the user
# makes logs/ and writes the log, and a link planted there can't send a root write elsewhere.
# An older setup.sh made logs/ as root; give that one back (-h: never follows a link).
if [ -d "$HERE/logs" ] && [ ! -L "$HERE/logs" ] && [ "$(stat -c %u "$HERE/logs")" = 0 ]; then
  chown -h "$TARGET_USER": "$HERE/logs"
fi
runuser -u "$TARGET_USER" -- mkdir -p "$HERE/logs"
exec > >(runuser -u "$TARGET_USER" -- tee "$LOG") 2>&1
echo "setup.sh ${1:-install}  $(date -Is)  user=$TARGET_USER"

case "${1:-install}" in
  install)
    set -e
    install -d -m 755 -o root -g root "$HELPER_DIR"
    # read as the user: a link planted in their folder can't make root copy out a root-only file
    src=$(mktemp)
    runuser -u "$TARGET_USER" -- cat "$HERE/helpers/fanmode.sh" > "$src"
    install -m 755 -o root -g root "$src" "$HELPER"
    rm -f "$src"
    tmp=$(mktemp)
    printf '%s ALL=(root) NOPASSWD: %s\n' "$TARGET_USER" "$HELPER" > "$tmp"
    if ! visudo -cqf "$tmp"; then rm -f "$tmp"; echo "sudoers rule failed validation; nothing changed"; exit 1; fi
    install -d -m 750 -o root -g root /etc/sudoers.d
    rm -f "$OLD_SUDOERS"
    install -m 440 -o root -g root "$tmp" "$SUDOERS"
    rm -f "$tmp"
    set +e
    echo "--- sudoers includes:"; grep -nE '^\s*[@#]include' /etc/sudoers
    echo "--- /etc/sudoers.d:"; ls -1 /etc/sudoers.d
    echo "--- rules for $TARGET_USER:"; sudo -l -U "$TARGET_USER" | sed -n '/may run/,$p'
    if runuser -u "$TARGET_USER" -- sudo -n "$HELPER" check >/dev/null 2>&1; then
      echo
      echo "Deckhand: setup done. The app can now change the fan mode."
    else
      echo
      echo "Setup installed the files, but sudo still wants a password for the helper."
      echo "Details are in $LOG."
      exit 1
    fi
    ;;
  remove)
    status=0
    if [ -x "$HELPER" ] && ! "$HELPER" off; then
      echo "The stock fan service didn't come back up: restart the Frame to be sure the fan runs normally."
      status=1
    fi
    rm -f "$SUDOERS" "$OLD_SUDOERS"
    rm -rf "$HELPER_DIR"
    echo "Deckhand: setup removed$([ "$status" = 0 ] && echo ", fan back to stock")."
    exit "$status"
    ;;
  *)
    sed -n '2,11p' "$0"
    exit 2
    ;;
esac
