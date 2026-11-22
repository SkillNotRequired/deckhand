// space_tool: where things are in your play space, for Deckhand (Wi-Fi map, ceiling).
//
//   space_tool bounds            boundary outline on the floor, standing space: "b x z" lines,
//                                then "walls N"
//   space_tool track <ms>        every <ms> (50..10000): "p <device> <valid> x y z fx fz" for the
//                                headset (device "hmd") and controllers ("left", "right"); fx/fz
//                                is the device's forward direction on the floor
//
// Standing space: metres, floor at y = 0, x right, -z forward (the same space as the
// boundary). Connects as a background app: never starts SteamVR, takes no focus, and
// stops when SteamVR quits or dies, stdout closes, or on SIGTERM (Deckhand reads the end
// of the output as "tracking stopped").
#include "common.h"

#include <algorithm>
#include <climits>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <vector>

static int bounds() {
    vr::IVRChaperoneSetup *setup = vr::VRChaperoneSetup();
    if (!setup) {
        fprintf(stderr, "SteamVR: no chaperone setup interface\n");
        return 1;
    }
    uint32_t n = 0;
    setup->GetLiveCollisionBoundsInfo(nullptr, &n);
    if (n > 4096) n = 0;  // not a believable number of walls
    std::vector<vr::HmdQuad_t> quads(n);
    if (n && !setup->GetLiveCollisionBoundsInfo(quads.data(), &n)) n = 0;
    n = std::min<uint32_t>(n, uint32_t(quads.size()));
    for (uint32_t i = 0; i < n; i++) {
        // Each wall quad: its floor end. The first corner within 1 cm of the lowest one, so float
        // noise between the two floor corners can't flip which end of the wall is reported.
        const vr::HmdVector3_t *c = quads[i].vCorners;
        float lowest = c[0].v[1];
        for (int k = 1; k < 4; k++) lowest = std::min(lowest, c[k].v[1]);
        int pick = 0;
        while (pick < 3 && c[pick].v[1] > lowest + 0.01f) pick++;
        printf("b %.3f %.3f\n", c[pick].v[0], c[pick].v[2]);
    }
    printf("walls %u\n", n);
    return 0;
}

static int track(long ms, pid_t server) {
    vr::IVRSystem *sys = vr::VRSystem();
    if (!sys) return 1;
    vr::TrackedDevicePose_t poses[vr::k_unMaxTrackedDeviceCount];
    double next_alive_check = mono_now() + kAliveCheckSec;
    while (!g_quit) {
        // A crashed vrserver sends no Quit: notice it ourselves (common.h) and stop.
        if (mono_now() >= next_alive_check) {
            next_alive_check = mono_now() + kAliveCheckSec;
            if (!process_exists(server)) {
                fprintf(stderr, "SteamVR went away without quitting\n");
                return 0;
            }
        }
        vr::VREvent_t ev;
        while (sys->PollNextEvent(&ev, sizeof ev))
            if (ev.eventType == vr::VREvent_Quit) {
                sys->AcknowledgeQuit_Exiting();
                return 0;
            }
        sys->GetDeviceToAbsoluteTrackingPose(vr::TrackingUniverseStanding, 0, poses, vr::k_unMaxTrackedDeviceCount);
        struct {
            const char *name;
            vr::TrackedDeviceIndex_t idx;
        } devs[] = {
            {"hmd", vr::k_unTrackedDeviceIndex_Hmd},
            {"left", sys->GetTrackedDeviceIndexForControllerRole(vr::TrackedControllerRole_LeftHand)},
            {"right", sys->GetTrackedDeviceIndexForControllerRole(vr::TrackedControllerRole_RightHand)},
        };
        for (const auto &d : devs) {
            if (d.idx >= vr::k_unMaxTrackedDeviceCount) continue;
            const vr::TrackedDevicePose_t &p = poses[d.idx];
            const auto &m = p.mDeviceToAbsoluteTracking.m;
            float fx = -m[0][2], fz = -m[2][2], len = std::sqrt(fx * fx + fz * fz);
            if (len > 1e-4f) {
                fx /= len;
                fz /= len;
            }
            if (printf("p %s %d %.3f %.3f %.3f %.3f %.3f\n", d.name, int(p.bPoseIsValid && p.bDeviceIsConnected),
                       m[0][3], m[1][3], m[2][3], fx, fz) < 0)
                return 0;
        }
        if (fflush(stdout) != 0) return 0;  // the reader has gone (SIGPIPE is ignored: helper_init)
        nap(double(ms) / 1000);
    }
    return 0;
}

int main(int argc, char **argv) {
    helper_init();
    long ms = 0;
    const bool want_bounds = argc == 2 && strcmp(argv[1], "bounds") == 0;
    const bool want_track = argc == 3 && strcmp(argv[1], "track") == 0 && parse_long(argv[2], LONG_MIN, LONG_MAX, ms);
    if (!want_bounds && !want_track) {
        fprintf(stderr, "usage: %s bounds | track <ms>\n", argv[0]);
        return 2;
    }
    // As a background app, VR_Init can't start SteamVR; there's just no point trying without it.
    const pid_t server = newest_vrserver(find_steamvr().servers);
    if (!server) {
        fprintf(stderr, "SteamVR is not running\n");
        return 1;
    }
    vr::EVRInitError err = vr::VRInitError_None;
    vr::VR_Init(&err, vr::VRApplication_Background);
    if (err != vr::VRInitError_None) {
        fprintf(stderr, "SteamVR: %s\n", vr::VR_GetVRInitErrorAsEnglishDescription(err));
        return 1;
    }
    const int rc = want_bounds ? bounds() : track(std::clamp(ms, 50L, 10000L), server);
    vr::VR_Shutdown();
    return rc;
}
