// ceiling_overlay: Deckhand's ceiling indicator (runs as frame-ceiling.service).
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
// SteamVR: it connects only through the safe connect gate (common.h), so it never starts
// SteamVR and waits for it instead. Pictures are uploaded only when first needed, at least a
// second apart, and let go of by the settle rule (common.h).
// Light on battery: 10 updates a second (1 a second while SteamVR is in standby), only while
// the service runs (i.e. some mode is on), and overlay settings are sent only when they change.
#include "common.h"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <fcntl.h>
#include <map>
#include <string>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

constexpr double kPi = 3.14159265358979323846;

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
static bool all_finite(const double *v, int n) {
    for (int i = 0; i < n; i++)
        if (!std::isfinite(v[i])) return false;
    return true;
}

// --------------------------------------------------------- Recline's R ---
// /recline_v1 (/dev/shm/recline_v1): magic at 0, the seqlock'd StateBlock at 192. We read its first
// 272 bytes (up to the end of R_t), so the file must be at least that big: reading a mapping past
// the end of its file is a SIGBUS.
struct Recline {
    static constexpr size_t kBytes = 272;
    const uint8_t *base = nullptr;  // the mapping, or null
    dev_t dev = 0;                  // which file is mapped: Recline may remove and recreate it
    ino_t ino = 0;
    double next_open = 0;           // while it's missing, shm_open is retried at most every 5 s
    double next_check = 0;          // once a second: has the file been replaced or removed?
    M4 last_good = identity();      // used when a read is torn or garbled

    void unmap() {
        if (base) munmap(const_cast<uint8_t *>(base), kBytes);
        base = nullptr;
    }

    void open_shm(double now) {
        if (base || now < next_open) return;
        next_open = now + 5;
        const int fd = shm_open("/recline_v1", O_RDONLY, 0);
        if (fd < 0) return;
        struct stat st;
        if (fstat(fd, &st) == 0 && st.st_size >= off_t(kBytes)) {
            void *p = mmap(nullptr, kBytes, PROT_READ, MAP_SHARED, fd, 0);
            if (p != MAP_FAILED) {
                base = static_cast<const uint8_t *>(p);
                dev = st.st_dev;
                ino = st.st_ino;
                next_check = now + 1;
            }
        }
        ::close(fd);
    }

    // Recline restarted (a new file: new inode), stopped (file removed) or shrank it: let go of the
    // old mapping, and look for the new file straight away.
    void check_file(double now) {
        if (!base || now < next_check) return;
        next_check = now + 1;
        struct stat st;
        if (stat("/dev/shm/recline_v1", &st) != 0 || st.st_dev != dev || st.st_ino != ino ||
            st.st_size < off_t(kBytes)) {
            unmap();
            next_open = 0;
        }
    }

    // R: identity if Recline isn't installed or isn't tilting the world. A torn or garbled read (or
    // a writer that keeps us out) gives the last good R rather than a jump.
    M4 get() {
        const double now = mono_now();
        check_file(now);
        open_shm(now);
        if (!base || *reinterpret_cast<const uint32_t *>(base) != 0x52434C4Eu) return last_good = identity();
        const uint8_t *block = base + 192;
        for (int attempt = 0; attempt < 50; attempt++) {
            const uint32_t s1 = __atomic_load_n(reinterpret_cast<const uint32_t *>(block), __ATOMIC_ACQUIRE);
            if (s1 & 1u) continue;  // being written
            const uint32_t flags = __atomic_load_n(reinterpret_cast<const uint32_t *>(block + 12), __ATOMIC_RELAXED);
            double q[4], t[3];
            memcpy(q, block + 24, sizeof q);
            memcpy(t, block + 56, sizeof t);
            __atomic_thread_fence(__ATOMIC_ACQUIRE);
            const uint32_t s2 = __atomic_load_n(reinterpret_cast<const uint32_t *>(block), __ATOMIC_RELAXED);
            if (s1 != s2) continue;  // changed while we read: again
            if (!(flags & 2u)) return last_good = identity();  // FLAG_TRANSFORM_ACTIVE
            if (!all_finite(q, 4) || !all_finite(t, 3)) return last_good;
            const double n = std::sqrt(q[0] * q[0] + q[1] * q[1] + q[2] * q[2] + q[3] * q[3]);
            if (std::fabs(n - 1) > 1e-3) return last_good;  // not a rotation
            for (double &c : q) c /= n;
            return last_good = from_quat(q, t);
        }
        return last_good;
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

// ------------------------------------------------------------- pictures ---
constexpr double kGridSize = 10.0;  // grid square, metres (covers a large room)
constexpr int kGridPx = 1024;

// A 1 m grid with 0.5 m lines over a `size`-metre square, transparent between lines.
static Image grid_texture(int S, double size) {
    Image im;
    im.w = im.h = uint32_t(S);
    im.px.assign(size_t(S) * S * 4, 0);
    const double per_m = S / size;
    auto line_at = [&](int i) {
        double m = i / per_m;
        double frac = std::fabs(m * 2 - std::round(m * 2));  // distance to the nearest 0.5 m line, in halves
        return frac * per_m / 2 < 1.6 || i < 4 || i >= S - 4;
    };
    for (int y = 0; y < S; y++)
        for (int x = 0; x < S; x++)
            if (line_at(x) || line_at(y)) {
                uint8_t *p = &im.px[(size_t(y) * S + x) * 4];
                p[0] = 102; p[1] = 204; p[2] = 255; p[3] = 230;
            }
    return im;
}

// A marker `s` metres from the head along real up (negative: down), facing the head.
static vr::HmdMatrix34_t marker_transform(const double upv[3], const double head[3], double s) {
    // +Z of the marker points back toward the head.
    const double sign = s > 0 ? 1 : -1;
    const double zx = -upv[0] * sign, zy = -upv[1] * sign, zz = -upv[2] * sign;
    double ax = 0, ay = 1, az = 0;  // a helper axis not parallel to z
    if (std::fabs(zy) > 0.9) {
        ay = 0;
        az = 1;
    }
    double xx = ay * zz - az * zy, xy = az * zx - ax * zz, xz = ax * zy - ay * zx;
    const double n = std::sqrt(xx * xx + xy * xy + xz * xz);
    xx /= n;
    xy /= n;
    xz /= n;
    const double yx = zy * xz - zz * xy, yy = zz * xx - zx * xz, yz = zx * xy - zy * xx;
    return {{{float(xx), float(yx), float(zx), float(head[0] + upv[0] * s)},
             {float(xy), float(yy), float(zy), float(head[1] + upv[1] * s)},
             {float(xz), float(yz), float(zz), float(head[2] + upv[2] * s)}}};
}

// ------------------------------------------------------------- overlays ---
enum Which { kGrid, kUp, kDown, kCount };
const char *const kKeys[kCount] = {"framesettings.ceiling.grid", "framesettings.ceiling.up", "framesettings.ceiling.down"};
const char *const kNames[kCount] = {"Ceiling", "Ceiling marker", "Floor marker"};
const char *const kFiles[kCount] = {nullptr, "/up.rgba", "/down.rgba"};  // marker pictures, in data_dir()

// The three overlays, and what SteamVR was last told about each: setters are only called on a change.
struct Overlays {
    OverlayConnection &vrc;
    vr::VROverlayHandle_t h[kCount] = {vr::k_ulOverlayHandleInvalid, vr::k_ulOverlayHandleInvalid,
                                       vr::k_ulOverlayHandleInvalid};
    bool textured[kCount] = {};     // its picture is uploaded
    bool stale[kCount] = {};        // a marker's file changed since its upload
    ino_t file_ino[kCount] = {};    // which version of the marker file was uploaded
    timespec file_mtime[kCount] = {};
    bool visible[kCount] = {};
    bool placed[kCount] = {};       // xf[] holds the transform last sent
    vr::HmdMatrix34_t xf[kCount] = {};
    float alpha = -1;               // the grid's alpha last sent
    double next_upload = 0;         // uploads are at least a second apart

    explicit Overlays(OverlayConnection &c) : vrc(c) {}

    bool upload_due(double now) {
        if (now < next_upload) return false;
        next_upload = now + 1;
        return true;
    }
    void set_visible(int i, bool on) {
        if (visible[i] == on) return;
        if (on)
            vrc.ov->ShowOverlay(h[i]);
        else
            vrc.ov->HideOverlay(h[i]);
        visible[i] = on;
    }
    void place(int i, const vr::HmdMatrix34_t &m) {
        if (placed[i] && memcmp(&xf[i], &m, sizeof m) == 0) return;
        vrc.ov->SetOverlayTransformAbsolute(h[i], vr::TrackingUniverseRawAndUncalibrated, &m);
        xf[i] = m;
        placed[i] = true;
    }
    void set_alpha(float a) {
        if (alpha >= 0 && std::fabs(a - alpha) <= 0.02f && !(a >= 1 && alpha < 1)) return;  // (do reach 1)
        vrc.ov->SetOverlayAlpha(h[kGrid], a);
        alpha = a;
    }
    // Uploads a marker's picture, if the app has drawn it.
    void upload_marker(int k) {
        const std::string path = data_dir() + kFiles[k];
        struct stat st;
        Image im;
        if (stat(path.c_str(), &st) != 0 || !load_rgba(path, im)) return;
        if (!vrc.upload(h[k], im)) return;
        textured[k] = true;
        stale[k] = false;
        file_ino[k] = st.st_ino;
        file_mtime[k] = st.st_mtim;
    }
    // A marker picture the app has redrawn since its upload: upload it again when it's next needed.
    void check_marker_files() {
        for (const int k : {kUp, kDown}) {
            struct stat st;
            if (textured[k] && stat((data_dir() + kFiles[k]).c_str(), &st) == 0 &&
                (st.st_ino != file_ino[k] || st.st_mtim.tv_sec != file_mtime[k].tv_sec ||
                 st.st_mtim.tv_nsec != file_mtime[k].tv_nsec))
                stale[k] = true;
        }
    }
};

// One connected stretch: until SteamVR quits or dies, or we're stopped.
static void run(OverlayConnection &vrc, Conf &conf, timespec &conf_stamp, Recline &recline) {
    Overlays o(vrc);
    for (int i = 0; i < kCount; i++) {
        const vr::EVROverlayError e = vrc.ov->CreateOverlay(kKeys[i], kNames[i], &o.h[i]);
        if (e == vr::VROverlayError_None) continue;
        fprintf(stderr, "CreateOverlay %s: %s\n", kKeys[i], vrc.ov->GetOverlayErrorNameFromEnum(e));
        // Nothing has been uploaded or shown yet: destroy what was made (no settle wait), disconnect.
        for (int j = 0; j < i; j++) vrc.ov->DestroyOverlay(o.h[j]);
        vrc.disconnect();
        nap(30);
        return;
    }
    vrc.ov->SetOverlayWidthInMeters(o.h[kGrid], float(kGridSize));
    for (const int k : {kUp, kDown}) {
        vrc.ov->SetOverlayWidthInMeters(o.h[k], 0.35f);
        vrc.ov->SetOverlaySortOrder(o.h[k], 150);
    }

    vr::IVRSystem *sys = vrc.sys;
    const bool debug = getenv("CEILING_DEBUG") != nullptr;
    bool standby = false;
    double next_second = 0;  // once-a-second chores
    vr::TrackedDeviceIndex_t hands[2] = {vr::k_unTrackedDeviceIndexInvalid, vr::k_unTrackedDeviceIndexInvalid};
    M4 Mst = identity(), MstInv = identity();  // raw -> standing, and back
    while (!g_quit) {
        if (vrc.server_died()) {
            fprintf(stderr, "SteamVR went away without quitting\n");
            vrc.drop(o.h, kCount);
            return;
        }
        bool quit = false;
        vr::VREvent_t ev;
        while (sys->PollNextEvent(&ev, sizeof ev)) {
            if (ev.eventType == vr::VREvent_Quit) {
                vrc.quit_received();
                quit = true;
                break;
            }
            if (ev.eventType == vr::VREvent_EnterStandbyMode) standby = true;
            if (ev.eventType == vr::VREvent_LeaveStandbyMode) standby = false;
        }
        if (quit) break;  // SteamVR is closing: straight to the close, no more calls

        const double now = mono_now();
        const bool chores = now >= next_second;
        if (chores) {
            next_second = now + 1;
            load_conf(conf, conf_stamp);
            Mst = from_hmd(sys->GetRawZeroPoseToStandingAbsoluteTrackingPose());
            MstInv = inverse(Mst);
            hands[0] = sys->GetTrackedDeviceIndexForControllerRole(vr::TrackedControllerRole_LeftHand);
            hands[1] = sys->GetTrackedDeviceIndexForControllerRole(vr::TrackedControllerRole_RightHand);
            o.check_marker_files();
        }

        const M4 R = recline.get();
        const M4 Rinv = inverse(R);
        const double upv[3] = {R.m[0][1], R.m[1][1], R.m[2][1]};  // real up, as seen in the tilted world
        const double tilt = std::acos(std::clamp(upv[1], -1.0, 1.0)) * 180 / kPi;  // degrees from upright

        // Grid: a horizontal square at the ceiling, in real space; turned 90 degrees about x so its
        // +Z normal points down (-Y).
        const M4 plane = {{{1, 0, 0, conf.cx}, {0, 0, -1, conf.height}, {0, 1, 0, conf.cz}}};
        const vr::HmdMatrix34_t grid_xf = to_hmd(mul(R, mul(MstInv, plane)));

        // How close the head and hands are to the real ceiling.
        const vr::TrackedDeviceIndex_t devs[3] = {vr::k_unTrackedDeviceIndex_Hmd, hands[0], hands[1]};
        uint32_t count = 1;
        for (const vr::TrackedDeviceIndex_t d : devs)
            if (d < vr::k_unMaxTrackedDeviceCount) count = std::max(count, d + 1);
        vr::TrackedDevicePose_t poses[vr::k_unMaxTrackedDeviceCount];
        sys->GetDeviceToAbsoluteTrackingPose(vr::TrackingUniverseRawAndUncalibrated, 0, poses, count);
        const M4 toReal = mul(Mst, Rinv);  // raw (tilted) -> real standing
        double nearest = 1e9;
        double head_raw[3] = {0, 0, 0};
        bool head_ok = false;
        for (int i = 0; i < 3; i++) {
            if (devs[i] >= vr::k_unMaxTrackedDeviceCount || !poses[devs[i]].bPoseIsValid) continue;
            const auto &pm = poses[devs[i]].mDeviceToAbsoluteTracking.m;
            const double p[3] = {pm[0][3], pm[1][3], pm[2][3]};
            double real[3];
            apply(toReal, p, real);
            nearest = std::min(nearest, conf.height - real[1]);
            if (i == 0) {
                memcpy(head_raw, p, sizeof p);
                head_ok = true;
            }
        }
        double alpha = conf.always ? conf.always_alpha : 0;
        if (conf.near && nearest < conf.near_dist)
            alpha = std::max(alpha, std::min(1.0, 1.0 - (nearest - 0.05) / std::max(0.05, conf.near_dist - 0.05)));

        // The grid's 4 MB picture is made and uploaded only once it's first needed.
        const bool want_grid = alpha > 0.01;
        if (want_grid && !o.textured[kGrid] && o.upload_due(now)) {
            Image grid = grid_texture(kGridPx, kGridSize);
            o.textured[kGrid] = vrc.upload(o.h[kGrid], grid);
        }
        if (want_grid && o.textured[kGrid]) {
            o.place(kGrid, grid_xf);
            o.set_alpha(float(std::min(1.0, alpha)));
        }
        o.set_visible(kGrid, want_grid && o.textured[kGrid]);
        if (debug && chores)
            fprintf(stderr, "recline tilt %.1f deg | nearest to ceiling %.2f m | grid alpha %.2f | head %d\n", tilt,
                    nearest, alpha, int(head_ok));

        // Real up / down markers while reclined (tilted more than 15 degrees).
        const bool want_markers = conf.upmarker && tilt > 15 && head_ok;
        for (const int k : {kUp, kDown}) {
            if (want_markers && (!o.textured[k] || o.stale[k]) && o.upload_due(now)) o.upload_marker(k);
            const bool on = want_markers && o.textured[k];
            if (on) o.place(k, marker_transform(upv, head_raw, k == kUp ? 1.2 : -1.0));
            o.set_visible(k, on);
        }
        nap(standby ? 1.0 : 0.1);
    }
    vrc.close(o.h, kCount);  // hide, settle, destroy, disconnect
}

int main() {
    helper_init();
    Conf conf;
    timespec conf_stamp{0, 0};
    load_conf(conf, conf_stamp);
    Recline recline;
    OverlayConnection vrc;  // outlives each connection: remembers a server that quit
    while (!g_quit) {
        // Wait for a settled SteamVR (never start it ourselves).
        if (!vrc.connect()) {
            nap(3);
            continue;
        }
        run(vrc, conf, conf_stamp, recline);
    }
    recline.unmap();
    return 0;
}
