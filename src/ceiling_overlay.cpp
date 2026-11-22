// ceiling_overlay: Deckhand' ceiling indicator (runs as frame-ceiling.service).
//
// Settings: ~/.config/frame-settings/ceiling.conf (key=value, re-read when it changes)
//   height=2.40      ceiling height above the floor, metres (standing space, not reclined)
//   cx=0 cz=0        centre of the play area on the floor (standing space)
//   near=1           grid fades in when a controller or the head is within near_dist
//   near_dist=0.40
//   always=0         grid always shown at always_alpha
//   always_alpha=0.18
//   upmarker=1       while Recline VR tilts the world: "Ceiling"/"Floor" markers toward
//                    the real ceiling and floor (pictures: ~/.local/share/frame-settings/
//                    ceiling/up.rgba, down.rgba, drawn by the app)
//
// Recline VR rotates every tracked pose by R (pose' = R * pose, driver world is
// gravity-aligned). Its current R is read from its shared memory /recline_v1
// (seqlock; StateBlock at offset 192: flags +12, R_q (w,x,y,z) +24, R_t +56).
// The ceiling lives in real space, so it is placed at R * (real position) in the
// raw tracking universe, and device heights are measured as R^-1 * (raw pose).
//
// Light on battery: 10 updates a second, only while the service runs (i.e. some
// mode is on); it never starts SteamVR and waits for it instead.
#include <openvr.h>

#include <atomic>
#include <cmath>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <dirent.h>
#include <fcntl.h>
#include <map>
#include <string>
#include <sys/mman.h>
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

// ------------------------------------------------------------------ math ---
struct M4 { double m[3][4]; };  // rigid 3x4, row-major like HmdMatrix34_t

static M4 identity() { return M4{{{1, 0, 0, 0}, {0, 1, 0, 0}, {0, 0, 1, 0}}}; }
static M4 from_hmd(const vr::HmdMatrix34_t &h) {
    M4 r;
    for (int i = 0; i < 3; i++)
        for (int j = 0; j < 4; j++) r.m[i][j] = h.m[i][j];
    return r;
}
static vr::HmdMatrix34_t to_hmd(const M4 &a) {
    vr::HmdMatrix34_t h;
    for (int i = 0; i < 3; i++)
        for (int j = 0; j < 4; j++) h.m[i][j] = float(a.m[i][j]);
    return h;
}
static M4 mul(const M4 &a, const M4 &b) {
    M4 r;
    for (int i = 0; i < 3; i++) {
        for (int j = 0; j < 3; j++) r.m[i][j] = a.m[i][0] * b.m[0][j] + a.m[i][1] * b.m[1][j] + a.m[i][2] * b.m[2][j];
        r.m[i][3] = a.m[i][0] * b.m[0][3] + a.m[i][1] * b.m[1][3] + a.m[i][2] * b.m[2][3] + a.m[i][3];
    }
    return r;
}
static M4 inverse(const M4 &a) {  // rigid: [R^T | -R^T t]
    M4 r;
    for (int i = 0; i < 3; i++)
        for (int j = 0; j < 3; j++) r.m[i][j] = a.m[j][i];
    for (int i = 0; i < 3; i++)
        r.m[i][3] = -(r.m[i][0] * a.m[0][3] + r.m[i][1] * a.m[1][3] + r.m[i][2] * a.m[2][3]);
    return r;
}
static void apply(const M4 &a, const double p[3], double out[3]) {
    for (int i = 0; i < 3; i++) out[i] = a.m[i][0] * p[0] + a.m[i][1] * p[1] + a.m[i][2] * p[2] + a.m[i][3];
}
static M4 from_quat(const double q[4], const double t[3]) {  // (w,x,y,z)
    const double w = q[0], x = q[1], y = q[2], z = q[3];
    return M4{{{1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y), t[0]},
               {2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x), t[1]},
               {2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y), t[2]}}};
}

// --------------------------------------------------------- Recline's R ---
struct Recline {
    const uint8_t *base = nullptr;

    void open_shm() {
        if (base) return;
        int fd = shm_open("/recline_v1", O_RDONLY, 0);
        if (fd < 0) return;
        void *p = mmap(nullptr, 4096, PROT_READ, MAP_SHARED, fd, 0);
        ::close(fd);
        if (p != MAP_FAILED) base = static_cast<const uint8_t *>(p);
    }

    // R (identity if Recline isn't installed or isn't reclining). angle_deg: how far it tilts.
    M4 get(double *angle_deg) {
        *angle_deg = 0;
        open_shm();
        if (!base || *reinterpret_cast<const uint32_t *>(base) != 0x52434C4Eu) return identity();
        const uint8_t *st = base + 192;
        for (int attempt = 0; attempt < 50; attempt++) {
            uint32_t s1 = __atomic_load_n(reinterpret_cast<const uint32_t *>(st), __ATOMIC_ACQUIRE);
            if (s1 & 1u) continue;
            uint32_t flags = __atomic_load_n(reinterpret_cast<const uint32_t *>(st + 12), __ATOMIC_RELAXED);
            double q[4], t[3];
            memcpy(q, st + 24, sizeof q);
            memcpy(t, st + 56, sizeof t);
            __atomic_thread_fence(__ATOMIC_ACQUIRE);
            uint32_t s2 = __atomic_load_n(reinterpret_cast<const uint32_t *>(st), __ATOMIC_RELAXED);
            if (s1 != s2) continue;
            if (!(flags & 2u)) return identity();  // FLAG_TRANSFORM_ACTIVE
            double n = std::sqrt(q[0] * q[0] + q[1] * q[1] + q[2] * q[2] + q[3] * q[3]);
            if (n < 0.5) return identity();
            for (double &c : q) c /= n;
            *angle_deg = 2 * std::acos(std::min(1.0, std::fabs(q[0]))) * 180 / M_PI;
            return from_quat(q, t);
        }
        return identity();
    }
};

// ------------------------------------------------------------- settings ---
struct Conf {
    double height = 2.40, cx = 0, cz = 0, near_dist = 0.40, always_alpha = 0.18;
    bool near = true, always = false, upmarker = true;
};

static std::string conf_path() { return std::string(getenv("HOME") ? getenv("HOME") : "") + "/.config/frame-settings/ceiling.conf"; }
static std::string data_dir() { return std::string(getenv("HOME") ? getenv("HOME") : "") + "/.local/share/frame-settings/ceiling"; }

static bool load_conf(Conf &c, timespec &stamp) {
    struct stat st;
    if (stat(conf_path().c_str(), &st) != 0) return false;
    if (st.st_mtim.tv_sec == stamp.tv_sec && st.st_mtim.tv_nsec == stamp.tv_nsec) return false;
    stamp = st.st_mtim;
    FILE *f = fopen(conf_path().c_str(), "r");
    if (!f) return false;
    char line[256];
    std::map<std::string, std::string> kv;
    while (fgets(line, sizeof line, f)) {
        char *eq = strchr(line, '=');
        if (!eq) continue;
        *eq = 0;
        std::string k = line, v = eq + 1;
        while (!v.empty() && (v.back() == '\n' || v.back() == '\r' || v.back() == ' ')) v.pop_back();
        kv[k] = v;
    }
    fclose(f);
    auto num = [&](const char *k, double d) { return kv.count(k) ? atof(kv[k].c_str()) : d; };
    c.height = num("height", 2.40);
    c.cx = num("cx", 0);
    c.cz = num("cz", 0);
    c.near_dist = num("near_dist", 0.40);
    c.always_alpha = num("always_alpha", 0.18);
    c.near = num("near", 1) != 0;
    c.always = num("always", 0) != 0;
    c.upmarker = num("upmarker", 1) != 0;
    return true;
}

static bool load_rgba(const std::string &path, std::vector<uint8_t> &px, uint32_t &w, uint32_t &h) {
    FILE *f = fopen(path.c_str(), "rb");
    if (!f) return false;
    uint32_t hdr[2];
    bool ok = fread(hdr, 4, 2, f) == 2 && hdr[0] && hdr[1] && hdr[0] <= 4096 && hdr[1] <= 4096;
    if (ok) {
        w = hdr[0];
        h = hdr[1];
        px.resize(size_t(w) * h * 4);
        ok = fread(px.data(), 1, px.size(), f) == px.size();
    }
    fclose(f);
    return ok;
}

// A 1 m grid with 0.5 m lines over a `size`-metre square, transparent between lines.
static std::vector<uint8_t> grid_texture(int S, double size) {
    std::vector<uint8_t> px(size_t(S) * S * 4, 0);
    const double per_m = S / size;
    auto line_at = [&](int i) {
        double m = i / per_m;
        double frac = std::fabs(m * 2 - std::round(m * 2));  // distance to the nearest 0.5 m line, in halves
        return frac * per_m / 2 < 1.6 || i < 4 || i >= S - 4;
    };
    for (int y = 0; y < S; y++)
        for (int x = 0; x < S; x++)
            if (line_at(x) || line_at(y)) {
                uint8_t *p = &px[(size_t(y) * S + x) * 4];
                p[0] = 102; p[1] = 204; p[2] = 255; p[3] = 230;
            }
    return px;
}

int main() {
    signal(SIGTERM, on_signal);
    signal(SIGINT, on_signal);
    const double kSize = 10.0;  // grid square, metres (covers a large room)
    Conf conf;
    timespec stamp{0, 0};
    load_conf(conf, stamp);
    Recline recline;

    while (!g_quit) {
        // Wait for SteamVR (never start it ourselves).
        while (!g_quit && !vrserver_running()) sleep(3);
        if (g_quit) break;
        vr::EVRInitError err = vr::VRInitError_None;
        vr::VR_Init(&err, vr::VRApplication_Overlay);
        if (err != vr::VRInitError_None) {
            sleep(5);
            continue;
        }
        vr::IVROverlay *ov = vr::VROverlay();
        vr::IVRSystem *sys = vr::VRSystem();
        vr::VROverlayHandle_t grid = 0, up = 0, down = 0;
        ov->CreateOverlay("framesettings.ceiling.grid", "Ceiling", &grid);
        ov->CreateOverlay("framesettings.ceiling.up", "Ceiling marker", &up);
        ov->CreateOverlay("framesettings.ceiling.down", "Floor marker", &down);
        const int S = 1024;
        std::vector<uint8_t> gpx = grid_texture(S, kSize);
        ov->SetOverlayRaw(grid, gpx.data(), S, S, 4);
        ov->SetOverlayWidthInMeters(grid, float(kSize));
        for (auto [h, file] : {std::pair{up, "/up.rgba"}, std::pair{down, "/down.rgba"}}) {
            std::vector<uint8_t> px;
            uint32_t w = 0, hh = 0;
            if (load_rgba(data_dir() + file, px, w, hh)) ov->SetOverlayRaw(h, px.data(), w, hh, 4);
            ov->SetOverlayWidthInMeters(h, 0.35f);
            ov->SetOverlaySortOrder(h, 150);
        }
        bool quit_vr = false;
        int tick = 0;
        while (!g_quit && !quit_vr) {
            vr::VREvent_t ev;
            while (sys->PollNextEvent(&ev, sizeof ev))
                if (ev.eventType == vr::VREvent_Quit) {
                    sys->AcknowledgeQuit_Exiting();
                    quit_vr = true;
                }
            if (++tick % 10 == 0) load_conf(conf, stamp);

            double angle = 0;
            const M4 R = recline.get(&angle);
            const M4 Rinv = inverse(R);
            const M4 Mst = from_hmd(sys->GetRawZeroPoseToStandingAbsoluteTrackingPose());  // raw -> standing
            const M4 MstInv = inverse(Mst);

            // Grid: a horizontal square at the ceiling, facing down, in real space.
            const double rx90[3][4] = {{1, 0, 0, 0}, {0, 0, -1, 0}, {0, 1, 0, 0}};  // +Z normal -> -Y (down)
            M4 plane;
            memcpy(plane.m, rx90, sizeof rx90);
            plane.m[0][3] = conf.cx;
            plane.m[1][3] = conf.height;
            plane.m[2][3] = conf.cz;
            vr::HmdMatrix34_t tg = to_hmd(mul(R, mul(MstInv, plane)));
            ov->SetOverlayTransformAbsolute(grid, vr::TrackingUniverseRawAndUncalibrated, &tg);

            // How close the head / controllers are to the real ceiling.
            vr::TrackedDevicePose_t poses[vr::k_unMaxTrackedDeviceCount];
            sys->GetDeviceToAbsoluteTrackingPose(vr::TrackingUniverseRawAndUncalibrated, 0, poses,
                                                 vr::k_unMaxTrackedDeviceCount);
            const M4 toReal = mul(Mst, Rinv);  // raw (tilted) -> real standing
            double nearest = 1e9;
            double head_raw[3] = {0, 0, 0};
            bool head_ok = false;
            for (vr::TrackedDeviceIndex_t i = 0; i < vr::k_unMaxTrackedDeviceCount; i++) {
                const auto cls = sys->GetTrackedDeviceClass(i);
                if (!poses[i].bPoseIsValid ||
                    (cls != vr::TrackedDeviceClass_HMD && cls != vr::TrackedDeviceClass_Controller))
                    continue;
                const auto &m = poses[i].mDeviceToAbsoluteTracking.m;
                double p[3] = {m[0][3], m[1][3], m[2][3]}, real[3];
                apply(toReal, p, real);
                nearest = std::min(nearest, conf.height - real[1]);
                if (cls == vr::TrackedDeviceClass_HMD) {
                    memcpy(head_raw, p, sizeof p);
                    head_ok = true;
                }
            }
            double alpha = conf.always ? conf.always_alpha : 0;
            if (conf.near && nearest < conf.near_dist)
                alpha = std::max(alpha, std::min(1.0, 1.0 - (nearest - 0.05) / std::max(0.05, conf.near_dist - 0.05)));
            if (alpha > 0.01) {
                ov->SetOverlayAlpha(grid, float(std::min(1.0, alpha)));
                ov->ShowOverlay(grid);
            } else {
                ov->HideOverlay(grid);
            }
            if (getenv("CEILING_DEBUG") && tick % 10 == 0)
                fprintf(stderr, "recline %.1f deg | nearest to ceiling %.2f m | grid alpha %.2f | head %d\n",
                        angle, nearest, alpha, int(head_ok));

            // Real up / down markers while reclined (> 15 degrees).
            if (conf.upmarker && angle > 15 && head_ok) {
                const double upv[3] = {R.m[0][1], R.m[1][1], R.m[2][1]};  // real up, as seen in the tilted world
                for (int k = 0; k < 2; k++) {
                    const double s = k == 0 ? 1.2 : -1.0;
                    // Face the head: +Z of the marker points back toward the head.
                    double zx = -upv[0] * (s > 0 ? 1 : -1), zy = -upv[1] * (s > 0 ? 1 : -1), zz = -upv[2] * (s > 0 ? 1 : -1);
                    double ax = 0, ay = 1, az = 0;  // pick a helper axis not parallel to z
                    if (std::fabs(zy) > 0.9) { ax = 0; ay = 0; az = 1; }
                    double xx = ay * zz - az * zy, xy = az * zx - ax * zz, xz = ax * zy - ay * zx;
                    double n = std::sqrt(xx * xx + xy * xy + xz * xz);
                    xx /= n; xy /= n; xz /= n;
                    double yx = zy * xz - zz * xy, yy = zz * xx - zx * xz, yz = zx * xy - zy * xx;
                    vr::HmdMatrix34_t mk = {{{float(xx), float(yx), float(zx), float(head_raw[0] + upv[0] * s)},
                                             {float(xy), float(yy), float(zy), float(head_raw[1] + upv[1] * s)},
                                             {float(xz), float(yz), float(zz), float(head_raw[2] + upv[2] * s)}}};
                    vr::VROverlayHandle_t h = k == 0 ? up : down;
                    ov->SetOverlayTransformAbsolute(h, vr::TrackingUniverseRawAndUncalibrated, &mk);
                    ov->ShowOverlay(h);
                }
            } else {
                ov->HideOverlay(up);
                ov->HideOverlay(down);
            }
            usleep(100000);
        }
        ov->DestroyOverlay(grid);
        ov->DestroyOverlay(up);
        ov->DestroyOverlay(down);
        vr::VR_Shutdown();
        if (quit_vr) sleep(3);
    }
    return 0;
}
