#!/bin/bash
# Builds Deckhand's native helpers on the Steam Frame (g++ and SteamVR's
# OpenVR headers ship with SteamOS on the Frame).
#   timer_overlay   head-locked timer / reminder popup, and the live Wi-Fi readout / mini-map
#   space_tool      boundary outline + headset/controller positions (Wi-Fi map, ceiling)
#   ceiling_overlay the ceiling indicator (frame-ceiling.service)
# src/common.h is shared by all three (the safe SteamVR connect gate, the overlay settle rule).
set -e
cd "$(dirname "$0")"
# Lowest CPU and disk priority, one file at a time: compiling while you're in VR
# must never starve SteamVR's tracking service (it crashed once during a build).
renice -n 19 -p $$ >/dev/null 2>&1 || true
ionice -c 3 -p $$ >/dev/null 2>&1 || true
# OpenVR's headers: where SteamVR keeps them today, or wherever an update moved them.
OVR_HDR=/opt/steamvr/tools/hellovr_vulkan_linux/src/openvr/headers
if [ ! -f "$OVR_HDR/openvr.h" ]; then
    found=$(find /opt/steamvr -name openvr.h -path '*headers*' 2>/dev/null | head -1)
    if [ -z "$found" ]; then
        echo "build.sh: can't find SteamVR's OpenVR header (openvr.h) under /opt/steamvr - is SteamVR installed?" >&2
        exit 1
    fi
    OVR_HDR=$(dirname "$found")
    echo "using OpenVR headers in $OVR_HDR"
fi
OVR_INC="-I$OVR_HDR"
OVR_LIB="-L/opt/steamvr/bin/linuxarm64 -lopenvr_api -Wl,-rpath,/opt/steamvr/bin/linuxarm64"
mkdir -p bin
# Sources edited on Windows may carry CRs.
sed -i 's/\r$//' src/*.cpp src/*.h
for t in timer_overlay space_tool ceiling_overlay; do
    # Build to a temp name and swap it in: a running copy keeps its old file.
    g++ -O2 -std=c++17 -Wall -Isrc -o "bin/$t.new" "src/$t.cpp" $OVR_INC $OVR_LIB
    mv -f "bin/$t.new" "bin/$t"
    echo "built: $t"
done
