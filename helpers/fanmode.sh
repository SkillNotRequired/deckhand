#!/bin/bash
# Fan mode helper for Deckhand (runs as root; installed by setup.sh).
#
# The Frame has one fan. Valve's fan service (deckard-fan-control) sets its
# speed from a temperature curve, but never below a fixed floor: 30 on
# battery, 40 while charging. "on" runs Valve's own controller from a copy in
# /etc with a lower floor and, optionally, the whole curve scaled down
# ("strength"). The "over 95 C -> full speed" protection is unchanged, and
# SteamOS still throttles the chip if it gets too hot. Lives in /etc, so it
# survives SteamOS updates; "off" puts everything back to stock.
#
#   fanmode on <floor> [charging_floor] [strength%]   floors 0-55, strength 50-100
#   fanmode off                                       stock fan control
#   fanmode uninstall                                 stock fan, then removes this helper
#                                                     and its sudo rule (Remove all)
#   fanmode poweroff                                  shut the Frame down (fallback
#                                                     when Steam's own shutdown fails)
#   fanmode check                                     prints "ok <version>"
set -eu
VERSION=4

SRC=/usr/share/deckard-fan-control
DST=/etc/deckard-fan-control-quiet
DROPIN_DIR=/etc/systemd/system/deckard-fan-control.service.d
DROPIN=$DROPIN_DIR/quiet.conf
SERVICE=deckard-fan-control.service

[ "$(id -u)" = 0 ] || { echo "fanmode must run as root" >&2; exit 1; }
is_int() { case "$1" in ''|*[!0-9]*) return 1 ;; esac; }

restart_checked() {
  systemctl daemon-reload
  systemctl restart $SERVICE
  sleep 3
  systemctl is-active --quiet $SERVICE
}

case "${1:-}" in
  check)
    echo "ok $VERSION"
    ;;
  poweroff)
    systemctl poweroff
    ;;
  on)
    FLOOR=${2:-0}
    CHG=${3:-$FLOOR}
    SCALE=${4:-100}
    is_int "$FLOOR" && [ "$FLOOR" -le 55 ] && is_int "$CHG" && [ "$CHG" -le 55 ] \
      || { echo "floors must be whole numbers 0-55" >&2; exit 2; }
    is_int "$SCALE" && [ "$SCALE" -ge 50 ] && [ "$SCALE" -le 100 ] \
      || { echo "strength must be a whole number 50-100" >&2; exit 2; }
    [ -f "$SRC/fancontrol.py" ] || { echo "Valve fan controller not found in $SRC" >&2; exit 3; }
    mkdir -p "$DST"
    cp -p "$SRC/fancontrol.py" "$SRC/PID.py" "$DST/"
    # Floors replaced; the curve's coefficients (quadratic on temperature,
    # linear feed-forward on power) multiplied by strength/100.
    {
      echo "# frame-settings: floor=$FLOOR charging=$CHG strength=$SCALE"
      awk -v floor="$FLOOR" -v chg="$CHG" -v k="$SCALE" '
        /^fan_min_speed:/          { print "fan_min_speed: " floor; next }
        /^fan_charging_min_speed:/ { print "fan_charging_min_speed: " chg; next }
        /^(A|B|C)_quad_control:|^(A|B)_ff_control:/ {
          printf "%s %s %.8g\n", $1, $2, $3 * k / 100; next }
        { print }' "$SRC/deckard-config.yaml"
    } > "$DST/deckard-config.yaml"
    mkdir -p "$DROPIN_DIR"
    printf '[Service]\nExecStart=\nExecStart=%s/fancontrol.py --run\nExecStopPost=\nExecStopPost=%s/fancontrol.py --stop\n' \
      "$DST" "$DST" > "$DROPIN"
    if ! restart_checked; then
      echo "quiet fan controller failed to start; restored stock" >&2
      rm -f "$DROPIN"
      systemctl daemon-reload
      systemctl restart $SERVICE
      exit 4
    fi
    echo "fan minimum $FLOOR (charging $CHG), strength $SCALE%"
    ;;
  off|uninstall)
    rm -f "$DROPIN"
    rmdir "$DROPIN_DIR" 2>/dev/null || true
    rm -rf "$DST"
    restart_checked || { echo "stock fan service did not come back up" >&2; exit 4; }
    echo "stock fan control"
    if [ "$1" = uninstall ]; then  # Deckhand's "Remove all": also this helper and its sudo rule
      rm -f /etc/sudoers.d/zz-frame-settings /etc/sudoers.d/frame-settings
      rm -rf /etc/frame-settings
      echo "fan helper and its sudo rule removed"
    fi
    ;;
  *)
    sed -n '2,19p' "$0" >&2
    exit 2
    ;;
esac
