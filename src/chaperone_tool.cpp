// chaperone_tool: small SteamVR boundary helper for Deckhand' room loading.
//
//   chaperone_tool reload    make the running SteamVR re-read chaperone_info.vrchap
//
// SteamVR keeps the boundary list in memory and writes it back over the file, so a
// room's boundaries merged into the file only count once SteamVR reloads it
// (IVRChaperoneSetup::ReloadFromDisk). Connects as a background app: it never
// starts SteamVR, and does nothing if SteamVR isn't running.
#include <openvr.h>

#include <cstdio>
#include <cstring>
#include <dirent.h>
#include <string>

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

int main(int argc, char **argv) {
    const bool reload = argc == 2 && strcmp(argv[1], "reload") == 0;
    const bool info = argc == 2 && strcmp(argv[1], "info") == 0;
    if (!reload && !info) {
        fprintf(stderr, "usage: %s reload|info\n", argv[0]);
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
    vr::IVRChaperoneSetup *setup = vr::VRChaperoneSetup();
    if (!setup) {
        fprintf(stderr, "no IVRChaperoneSetup\n");
        vr::VR_Shutdown();
        return 1;
    }
    if (reload) {
        setup->ReloadFromDisk(vr::EChaperoneConfigFile_Live);
        printf("reloaded\n");
    } else {  // the boundary SteamVR is using right now (live, in memory)
        uint32_t n = 0;
        setup->GetLiveCollisionBoundsInfo(nullptr, &n);
        float w = 0, h = 0;
        vr::VRChaperone()->GetPlayAreaSize(&w, &h);
        printf("live walls %u, play area %.2f x %.2f m, calibration state %d\n", n, w, h,
               int(vr::VRChaperone()->GetCalibrationState()));
    }
    vr::VR_Shutdown();
    return 0;
}
