#!/usr/bin/env bash
# screenshots.sh <name> <WxH>: walk every screen on the simulator in a headless KWin session (no window on your
# screen) and save a picture of each. Never shoot a real camera: its screens show the serial and nearby network names.
# Needs kwin_wayland and dbus-run-session. The pictures land in ${QUAK_TOUR_OUT:-/tmp/quak-tour}/<name>/shots.
S=${QUAK_TOUR_OUT:-/tmp/quak-tour}
Q=$(cd "$(dirname "$0")/.." && pwd)
T=$S/$1; rm -rf "$T"; mkdir -p "$T/cfg" "$T/state" "$T/shots"
W=${2%x*}; H=${2#*x}
cd /tmp && timeout 300 env -u DISPLAY -u WAYLAND_DISPLAY dbus-run-session -- kwin_wayland --virtual --width $((W+200)) --height $((H+200)) \
  --socket "quak-tour-$1" --xwayland --exit-with-session "env QUAK_SIM=1 QUAK_TOUR=$T/shots QUAK_TOUR_SIZE=$2 QUAK_TOUR_THEME=${QUAK_TOUR_THEME:-} QUAK_CONFIG_DIR=$T/cfg QUAK_STATE_DIR=$T/state QUAK_SIM_STATE=$T/sim.json QUAK_DOWNLOADS=$T/dl $Q/.venv/bin/quak-gui" > "$T/kwin.log" 2>&1
ls "$T/shots" | wc -l
