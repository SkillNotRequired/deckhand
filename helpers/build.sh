#!/bin/bash
# Builds Deckhand' native helpers on the Steam Frame (g++ and SteamVR's
# OpenVR headers ship with SteamOS on the Frame).
#   timer_overlay   head-locked timer / reminder popup, and the live Wi-Fi readout
#   chaperone_tool  SteamVR boundary reload + live boundary info
#   space_tool      boundary outline + headset/controller positions (Wi-Fi map, ceiling)
#   ceiling_overlay the ceiling indicator (frame-ceiling.service)
set -e
cd "$(dirname "$0")"
OVR_INC="-I/opt/steamvr/tools/hellovr_vulkan_linux/src/openvr/headers"
OVR_LIB="-L/opt/steamvr/bin/linuxarm64 -lopenvr_api -Wl,-rpath,/opt/steamvr/bin/linuxarm64"
mkdir -p bin
for t in timer_overlay chaperone_tool space_tool ceiling_overlay; do
    sed -i 's/\r$//' "src/$t.cpp"
    # Build to a temp name and swap it in: a running copy keeps its old file.
    g++ -O2 -std=c++17 -Wall -o "bin/$t.new" "src/$t.cpp" $OVR_INC $OVR_LIB
    mv -f "bin/$t.new" "bin/$t"
    echo "built: $t"
done
