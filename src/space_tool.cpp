// space_tool: where things are in your play space, for Deckhand (Wi-Fi map, ceiling).
//
//   space_tool bounds            boundary outline on the floor, standing space: "b x z" lines
//   space_tool track <ms>        every <ms>: "p <device> <valid> x y z fx fz" for the headset
//                                (device "hmd") and controllers ("left", "right"); fx/fz is
//                                the device's forward direction on the floor; plus for each
//                                controller "k <device> <trigger 0..1> <pressed-button mask hex>"
//
// Standing space: metres, floor at y = 0, x right, -z forward (the same space as the
// boundary). Connects as a background app: never starts SteamVR, takes no focus,
// and stops when SteamVR quits, stdout closes, or on SIGTERM.
#include <openvr.h>

#include <cmath>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <dirent.h>
#include <string>
#include <unistd.h>
#include <vector>

static volatile sig_atomic_t g_quit = 0;
static void on_signal(int) { g_quit = 1; }

static bool vrserver_running() {
    DIR *d = opendir("/proc");
    if (!d) return false;
    bool found = false;
    while (dirent *e = readdir(d)) {
        if (e->d_name[0] < '0' || e->d_name[0] > '9') continue;
        FILE *f = fopen((std::string("/proc/") + e->d_name + "/comm").c_str(), "r");
        if (!f) continue;
        char comm[64] = {0};
        found = fgets(comm, sizeof comm, f) && strcmp(comm, "vrserver\n") == 0;
        fclose(f);
        if (found) break;
    }
    closedir(d);
    return found;
}

static int bounds() {
    uint32_t n = 0;
    vr::VRChaperoneSetup()->GetLiveCollisionBoundsInfo(nullptr, &n);
    std::vector<vr::HmdQuad_t> quads(n);
    if (n && !vr::VRChaperoneSetup()->GetLiveCollisionBoundsInfo(quads.data(), &n)) n = 0;
    for (uint32_t i = 0; i < n; i++) {
        // each wall quad: pick its lowest corner (the floor end)
        const vr::HmdVector3_t *lo = &quads[i].vCorners[0];
        for (const auto &c : quads[i].vCorners)
            if (c.v[1] < lo->v[1]) lo = &c;
        printf("b %.3f %.3f\n", lo->v[0], lo->v[2]);
    }
    printf("walls %u\n", n);
    return 0;
}

static int track(int ms) {
    signal(SIGTERM, on_signal);
    signal(SIGINT, on_signal);
    signal(SIGPIPE, on_signal);
    vr::IVRSystem *sys = vr::VRSystem();
    vr::TrackedDevicePose_t poses[vr::k_unMaxTrackedDeviceCount];
    while (!g_quit) {
        vr::VREvent_t ev;
        while (sys->PollNextEvent(&ev, sizeof ev))
            if (ev.eventType == vr::VREvent_Quit) {
                sys->AcknowledgeQuit_Exiting();
                return 0;
            }
        sys->GetDeviceToAbsoluteTrackingPose(vr::TrackingUniverseStanding, 0, poses, vr::k_unMaxTrackedDeviceCount);
        struct { const char *name; vr::TrackedDeviceIndex_t idx; } devs[] = {
            {"hmd", vr::k_unTrackedDeviceIndex_Hmd},
            {"left", sys->GetTrackedDeviceIndexForControllerRole(vr::TrackedControllerRole_LeftHand)},
            {"right", sys->GetTrackedDeviceIndexForControllerRole(vr::TrackedControllerRole_RightHand)},
        };
        for (const auto &d : devs) {
            if (d.idx >= vr::k_unMaxTrackedDeviceCount) continue;
            const vr::TrackedDevicePose_t &p = poses[d.idx];
            const auto &m = p.mDeviceToAbsoluteTracking.m;
            float fx = -m[0][2], fz = -m[2][2], len = std::sqrt(fx * fx + fz * fz);
            if (len > 1e-4f) { fx /= len; fz /= len; }
            if (printf("p %s %d %.3f %.3f %.3f %.3f %.3f\n", d.name, int(p.bPoseIsValid && p.bDeviceIsConnected),
                       m[0][3], m[1][3], m[2][3], fx, fz) < 0)
                return 0;
            vr::VRControllerState_t st{};
            if (d.idx != vr::k_unTrackedDeviceIndex_Hmd && sys->GetControllerState(d.idx, &st, sizeof st))
                printf("k %s %.2f %llx\n", d.name, st.rAxis[1].x, (unsigned long long)st.ulButtonPressed);
        }
        if (fflush(stdout) != 0) return 0;
        usleep(useconds_t(ms) * 1000);
    }
    return 0;
}

int main(int argc, char **argv) {
    const bool want_bounds = argc == 2 && strcmp(argv[1], "bounds") == 0;
    const bool want_track = argc == 3 && strcmp(argv[1], "track") == 0;
    if (!want_bounds && !want_track) {
        fprintf(stderr, "usage: %s bounds | track <ms>\n", argv[0]);
        return 2;
    }
    if (!vrserver_running()) {
        fprintf(stderr, "SteamVR is not running\n");
        return 1;
    }
    vr::EVRInitError err = vr::VRInitError_None;
    vr::VR_Init(&err, vr::VRApplication_Background);
    if (err != vr::VRInitError_None) {
        fprintf(stderr, "SteamVR: %s\n", vr::VR_GetVRInitErrorAsEnglishDescription(err));
        return 1;
    }
    int ms = want_track ? atoi(argv[2]) : 0;
    int rc = want_bounds ? bounds() : track(ms < 50 ? 50 : ms);
    vr::VR_Shutdown();
    return rc;
}
