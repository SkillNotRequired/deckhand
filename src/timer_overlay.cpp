// timer_overlay: the in-headset part of Deckhand' kitchen timer and reminders.
//
//   timer_overlay <end_epoch_seconds> <image_dir> [popup_seconds]
//   timer_overlay --selftest <file.rgba>     (loads an image into a hidden overlay)
//
// Sleeps until `popup_seconds` (default 60; 0 for reminders) before the end,
// then shows a countdown pinned to the top-centre of your view (a SteamVR
// overlay locked to the headset) from <image_dir>/NN.rgba (NN = seconds left).
// At zero it shows <image_dir>/done.rgba, plays the alarm a few times and keeps
// it up for 30 s. SIGTERM (Cancel in the app) hides the overlay and exits.
//
// Images are raw RGBA (8-byte header: uint32 width, uint32 height, little
// endian) uploaded with SetOverlayRaw: the picture is swapped in place, with no
// decoding and no blank frame between seconds (SetOverlayFromFile flickered and
// dropped seconds).
//
// Light on battery: while waiting it sleeps in long chunks (signals still wake
// it), and it only connects to SteamVR for the popup itself. It never starts
// SteamVR (VR_Init would otherwise launch a display-less SteamVR); without
// SteamVR the alarm still sounds.
#include <openvr.h>

#include <csignal>
#include <cstdint>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <dirent.h>
#include <string>
#include <sys/stat.h>
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

static double now() {
    timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

static bool exists(const std::string &p) {
    struct stat st;
    return stat(p.c_str(), &st) == 0;
}

static void play_alarm() {
    const char *sounds[] = {"/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga",
                            "/usr/share/sounds/ocean/stereo/alarm-clock-elapsed.oga",
                            "/usr/share/sounds/freedesktop/stereo/complete.oga"};
    for (const char *s : sounds)
        if (exists(s)) {
            std::string cmd = std::string("paplay '") + s + "' >/dev/null 2>&1 &";
            if (std::system(cmd.c_str()) != 0) fprintf(stderr, "paplay failed\n");
            return;
        }
}

struct Image {
    uint32_t w = 0, h = 0;
    std::vector<uint8_t> px;
};

static bool load_rgba(const std::string &path, Image &im) {
    FILE *f = fopen(path.c_str(), "rb");
    if (!f) return false;
    uint32_t hdr[2];
    bool ok = fread(hdr, 4, 2, f) == 2 && hdr[0] > 0 && hdr[1] > 0 && hdr[0] <= 4096 && hdr[1] <= 4096;
    if (ok) {
        im.w = hdr[0];
        im.h = hdr[1];
        im.px.resize(size_t(im.w) * im.h * 4);
        ok = fread(im.px.data(), 1, im.px.size(), f) == im.px.size();
    }
    fclose(f);
    return ok;
}

struct Overlay {
    vr::IVROverlay *ov = nullptr;
    vr::VROverlayHandle_t h = vr::k_ulOverlayHandleInvalid;
    bool shown = false;
    const char *key = "framesettings.timer";
    float up_tan = 0.2493f;  // tan(14 deg): how far above the centre of the view
    float right = 0.0f;      // metres right of centre (negative = left)
    float width = 0.42f;     // metres
    bool face_eye = false;   // turn the panel to face the eye (for off-centre panels)

    bool open() {
        if (ov) return true;
        if (!vrserver_running()) return false;
        vr::EVRInitError err = vr::VRInitError_None;
        vr::VR_Init(&err, vr::VRApplication_Overlay);
        if (err != vr::VRInitError_None) {
            fprintf(stderr, "SteamVR: %s\n", vr::VR_GetVRInitErrorAsEnglishDescription(err));
            return false;
        }
        ov = vr::VROverlay();
        vr::EVROverlayError oe = ov->CreateOverlay(key, "Deckhand", &h);
        if (oe != vr::VROverlayError_None) {
            fprintf(stderr, "CreateOverlay: %s\n", ov->GetOverlayErrorNameFromEnum(oe));
            close();
            return false;
        }
        // 1.1 m ahead, up_tan up (14 degrees for timers), 0.42 m wide (about 22 degrees).
        const float d = 1.1f, up = d * up_tan;
        vr::HmdMatrix34_t m = {{{1, 0, 0, right}, {0, 1, 0, up}, {0, 0, 1, -d}}};
        if (face_eye) {  // columns = the panel's x, y, z axes; z points back at the eye
            float p[3] = {right, up, -d}, n = std::sqrt(p[0] * p[0] + p[1] * p[1] + p[2] * p[2]);
            float z[3] = {-p[0] / n, -p[1] / n, -p[2] / n};
            float x[3] = {z[2], 0, -z[0]};  // up x z
            float xl = std::sqrt(x[0] * x[0] + x[2] * x[2]);
            x[0] /= xl; x[2] /= xl;
            float y[3] = {z[1] * x[2] - z[2] * x[1], z[2] * x[0] - z[0] * x[2], z[0] * x[1] - z[1] * x[0]};
            for (int r = 0; r < 3; r++) {
                m.m[r][0] = x[r];
                m.m[r][1] = y[r];
                m.m[r][2] = z[r];
            }
        }
        ov->SetOverlayWidthInMeters(h, width);
        ov->SetOverlayTransformTrackedDeviceRelative(h, vr::k_unTrackedDeviceIndex_Hmd, &m);
        ov->SetOverlaySortOrder(h, 200);
        ov->SetOverlayAlpha(h, 0.95f);
        shown = false;
        return true;
    }

    void show(const std::string &path) {
        Image im;
        if (!load_rgba(path, im)) {
            fprintf(stderr, "can't read %s\n", path.c_str());
            return;
        }
        if (!ov && !open()) return;
        vr::EVROverlayError oe = ov->SetOverlayRaw(h, im.px.data(), im.w, im.h, 4);
        if (oe != vr::VROverlayError_None) fprintf(stderr, "SetOverlayRaw: %s\n", ov->GetOverlayErrorNameFromEnum(oe));
        if (!shown) {  // show once, after the first picture is in: no empty frame
            ov->ShowOverlay(h);
            shown = true;
        }
    }

    void poll() {  // SteamVR quitting: let go of it (and reconnect on the next show if it comes back)
        if (!ov) return;
        vr::VREvent_t ev;
        while (vr::VRSystem() && vr::VRSystem()->PollNextEvent(&ev, sizeof ev))
            if (ev.eventType == vr::VREvent_Quit) {
                vr::VRSystem()->AcknowledgeQuit_Exiting();
                close();
                return;
            }
    }

    void close() {
        if (ov && h != vr::k_ulOverlayHandleInvalid) ov->DestroyOverlay(h);
        if (ov) vr::VR_Shutdown();
        ov = nullptr;
        h = vr::k_ulOverlayHandleInvalid;
        shown = false;
    }
};

// Loads an image into a hidden overlay: checks the SteamVR side without showing anything.
static int selftest(const char *path) {
    if (!vrserver_running()) {
        printf("selftest: SteamVR not running\n");
        return 1;
    }
    Image im;
    if (!load_rgba(path, im)) {
        printf("selftest: can't read %s\n", path);
        return 1;
    }
    vr::EVRInitError err = vr::VRInitError_None;
    vr::VR_Init(&err, vr::VRApplication_Overlay);
    if (err != vr::VRInitError_None) {
        printf("selftest: VR_Init: %s\n", vr::VR_GetVRInitErrorAsEnglishDescription(err));
        return 1;
    }
    vr::IVROverlay *ov = vr::VROverlay();
    vr::VROverlayHandle_t h;
    vr::EVROverlayError oe = ov->CreateOverlay("framesettings.timer.selftest", "selftest", &h);
    printf("selftest: CreateOverlay %s\n", ov->GetOverlayErrorNameFromEnum(oe));
    int rc = oe == vr::VROverlayError_None ? 0 : 1;
    if (rc == 0) {
        for (int i = 0; i < 3 && rc == 0; i++) {  // a few uploads, like consecutive seconds
            oe = ov->SetOverlayRaw(h, im.px.data(), im.w, im.h, 4);
            printf("selftest: SetOverlayRaw %ux%u %s\n", im.w, im.h, ov->GetOverlayErrorNameFromEnum(oe));
            rc = oe == vr::VROverlayError_None ? 0 : 1;
        }
        printf("selftest: visible %d\n", int(ov->IsOverlayVisible(h)));
        ov->DestroyOverlay(h);
    }
    vr::VR_Shutdown();
    return rc;
}

// --live <file.rgba>: show <file> in the same top-centre spot and re-upload it whenever it
// changes, until SIGTERM (Deckhand uses it for the Wi-Fi readout while mapping).
// --minimap <file.rgba>: the same, as a small panel low on the left (the house walk's mini-map).
static int live(const char *path, bool minimap) {
    signal(SIGTERM, on_signal);
    signal(SIGINT, on_signal);
    Overlay o;
    o.key = "framesettings.live";
    o.up_tan = 0.0875f;  // tan(5 deg): a little above centre, under any timer popup
    if (minimap) {       // --minimap: lower left of the view, like a game's mini-map
        o.key = "framesettings.minimap";
        o.up_tan = -0.29f;
        o.right = -0.36f;
        o.width = 0.30f;
        o.face_eye = true;
    }
    time_t last_mtime = 0;
    long last_ns = -1;
    while (!g_quit) {
        struct stat st;
        if (stat(path, &st) == 0 && (st.st_mtime != last_mtime || st.st_mtim.tv_nsec != last_ns)) {
            last_mtime = st.st_mtime;
            last_ns = st.st_mtim.tv_nsec;
            o.show(path);
        }
        o.poll();
        usleep(200000);
    }
    o.close();
    return 0;
}

int main(int argc, char **argv) {
    if (argc == 3 && strcmp(argv[1], "--selftest") == 0) return selftest(argv[2]);
    if (argc == 3 && strcmp(argv[1], "--live") == 0) return live(argv[2], false);
    if (argc == 3 && strcmp(argv[1], "--minimap") == 0) return live(argv[2], true);
    if (argc < 3) {
        fprintf(stderr, "usage: %s <end_epoch> <image_dir> [popup_seconds]\n", argv[0]);
        return 2;
    }
    const double end = atof(argv[1]);
    const std::string dir = argv[2];
    const int popup = argc > 3 ? atoi(argv[3]) : 60;
    signal(SIGTERM, on_signal);
    signal(SIGINT, on_signal);

    // Wait in chunks of up to 30 s (a signal cuts the sleep short).
    while (!g_quit && now() < end - popup) {
        const double wait = end - popup - now();
        usleep(useconds_t((wait > 30.0 ? 30.0 : (wait > 0 ? wait : 0)) * 1e6));
    }
    if (g_quit) return 0;

    Overlay o;
    int last = -1;
    while (!g_quit) {
        const double left = end - now();
        if (left <= 0) break;
        const int secs = int(left + 0.999);  // ceil: show 60..1
        if (secs != last) {
            char name[32];
            snprintf(name, sizeof name, "%02d.rgba", secs > 60 ? 60 : secs);
            o.show(dir + "/" + name);
            last = secs;
        }
        o.poll();
        // Sleep to just past the next whole second, so every second is shown on time.
        const double frac = (end - now()) - double(int(end - now()));
        usleep(useconds_t((frac > 0.02 ? frac : 0.02) * 1e6) + 5000);
    }
    if (!g_quit) {
        o.show(dir + "/done.rgba");
        for (int i = 0; i < 60 && !g_quit; i++) {  // 30 s up; alarm at 0, 4 and 8 s
            if (i == 0 || i == 8 || i == 16) play_alarm();
            o.poll();
            usleep(500000);
        }
    }
    o.close();
    return 0;
}
