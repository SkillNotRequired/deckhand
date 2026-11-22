#!/bin/bash
# One-time setup for Deckhand. Run in a terminal on the Frame:
#
#   sudo ~/Deckhand/setup.sh            install   (use the folder Deckhand is in)
#   sudo ~/Deckhand/setup.sh remove     undo (also puts the fan back to stock)
#
# Installs the fan helper as a root-owned file (/etc/frame-settings/fanmode) and
# a sudoers rule that lets your user run exactly that helper without a password,
# so the app can change the fan mode from inside VR. Nothing else gets
# password-free access, and the helper only accepts "on <floor> [<floor>] [<strength>]",
# "off", "poweroff" or "check". Screenshots need no setup.
#
# sudo applies the LAST matching rule, and reads /etc/sudoers.d in name order,
# so the rule file is named zz-* to come after SteamOS's own wheel rule.
set -u

[ "$(id -u)" = 0 ] || { echo "run with: sudo $0 ${1:-}" >&2; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)
TARGET_USER=${SUDO_USER:-}
[ -n "$TARGET_USER" ] && [ "$TARGET_USER" != root ] || { echo "run it with sudo from your own user" >&2; exit 1; }
HELPER_DIR=/etc/frame-settings
HELPER=$HELPER_DIR/fanmode
SUDOERS=/etc/sudoers.d/zz-frame-settings
OLD_SUDOERS=/etc/sudoers.d/frame-settings
mkdir -p "$HERE/logs"
LOG=$HERE/logs/setup.log

# Everything below also goes to setup.log (readable without root) for troubleshooting.
exec > >(tee "$LOG") 2>&1
chown "$TARGET_USER" "$LOG" 2>/dev/null
echo "setup.sh ${1:-install}  $(date -Is)  user=$TARGET_USER"

case "${1:-install}" in
  install)
    set -e
    install -d -m 755 -o root -g root "$HELPER_DIR"
    install -m 755 -o root -g root "$HERE/helpers/fanmode.sh" "$HELPER"
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
    [ -x "$HELPER" ] && "$HELPER" off
    rm -f "$SUDOERS" "$OLD_SUDOERS"
    rm -rf "$HELPER_DIR"
    echo "Deckhand: setup removed, fan back to stock."
    ;;
  *)
    sed -n '2,11p' "$0"
    exit 2
    ;;
esac
