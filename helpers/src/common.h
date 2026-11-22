// common.h: what Deckhand's native SteamVR helpers (timer_overlay, space_tool, ceiling_overlay) share.
//
// Every rule in here comes from something that went wrong on the Steam Frame:
//  - Starting SteamVR. An overlay client (VRApplication_Overlay) STARTS SteamVR when no server
//    answers -- outside steamvr.service -- and connecting while SteamVR is starting or stopping has
//    upset its tracking service (and with it the camera passthrough). So the overlay helpers connect
//    only through the safe connect gate (OverlayConnection::gate_open). space_tool connects as a
//    background app, which can't start SteamVR; it only checks that SteamVR runs.
//  - Settling. vrcompositor reads SetOverlayRaw uploads asynchronously: destroying the overlay or
//    disconnecting right after an upload crashed it (SIGBUS). So an overlay is hidden first, and
//    nothing is destroyed or disconnected until kSettleSec after the last upload.
//  - Liveness. A vrserver that crashes sends no VREvent_Quit, so a connected helper checks every
//    couple of seconds that the server it connected to still exists.
//  - Environment. Started by Deckhand that Steam launched, a helper inherits SteamAppId/SteamGameId,
//    and SteamVR then treats it as that Steam app.
#pragma once

#include <openvr.h>

#include <cerrno>
#include <cmath>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <dirent.h>
#include <fcntl.h>
#include <string>
#include <sys/types.h>
#include <unistd.h>
#include <utility>
#include <vector>

// --------------------------------------------------------------- startup ---
// Set by SIGTERM / SIGINT / SIGHUP (systemctl stop, Cancel in the app): finish properly, then exit.
inline volatile sig_atomic_t g_quit = 0;
inline void on_stop_signal(int) { g_quit = 1; }

// The first thing every main() does.
inline void helper_init() {
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sigemptyset(&sa.sa_mask);
    // SA_RESTART, like signal(): OpenVR's own calls carry on after the handler; our sleeps
    // (clock_nanosleep) still wake up with EINTR, which is all we need.
    sa.sa_flags = SA_RESTART;
    sa.sa_handler = on_stop_signal;
    sigaction(SIGTERM, &sa, nullptr);
    sigaction(SIGINT, &sa, nullptr);
    sigaction(SIGHUP, &sa, nullptr);
    // A closed output pipe must not kill a helper (perhaps right after an upload): the write fails instead.
    sa.sa_handler = SIG_IGN;
    sigaction(SIGPIPE, &sa, nullptr);
    // Inherited when Steam started Deckhand: SteamVR would take the helper for that Steam app.
    unsetenv("SteamAppId");
    unsetenv("SteamGameId");
}

// ------------------------------------------------------------------ time ---
inline double clock_seconds(clockid_t clock) {
    timespec ts;
    clock_gettime(clock, &ts);
    return double(ts.tv_sec) + double(ts.tv_nsec) / 1e9;
}
inline double mono_now() { return clock_seconds(CLOCK_MONOTONIC); }  // for intervals: never jumps
inline double wall_now() { return clock_seconds(CLOCK_REALTIME); }   // epoch seconds (when a timer ends)

// Rounded up to the next nanosecond, so a sleep until `t` never ends before `t`.
inline timespec to_timespec(double t) {
    timespec ts;
    ts.tv_sec = time_t(t);
    long ns = long(std::ceil((t - double(ts.tv_sec)) * 1e9));
    if (ns >= 1000000000L) {
        ts.tv_sec += 1;
        ns -= 1000000000L;
    }
    ts.tv_nsec = ns < 0 ? 0 : ns;
    return ts;
}

// Sleeps `seconds`, all of them: a signal doesn't cut it short (usleep would return early, e.g. on
// the very SIGTERM that started a close, and the settle wait must not). Sleeping to an absolute
// deadline, a sleep resumed after EINTR still ends on time.
inline void sleep_full(double seconds) {
    if (seconds <= 0) return;
    const timespec deadline = to_timespec(mono_now() + seconds);
    while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &deadline, nullptr) == EINTR) {
    }
}

// Waits up to `seconds`, but returns early once a stop signal has arrived. False if one has.
inline bool nap(double seconds) {
    if (seconds > 0 && !g_quit) {
        const timespec ts = to_timespec(mono_now() + seconds);
        while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, nullptr) == EINTR && !g_quit) {
        }
    }
    return !g_quit;
}

// --------------------------------------------------------------- parsing ---
// Whole-argument number parsing: empty strings, trailing junk, overflow and inf/nan are refused.
inline bool parse_double(const char *s, double &out) {
    char *rest = nullptr;
    errno = 0;
    const double v = strtod(s, &rest);
    if (rest == s || *rest || errno == ERANGE || !std::isfinite(v)) return false;
    out = v;
    return true;
}
inline bool parse_long(const char *s, long lo, long hi, long &out) {
    char *rest = nullptr;
    errno = 0;
    const long v = strtol(s, &rest, 10);
    if (rest == s || *rest || errno == ERANGE || v < lo || v > hi) return false;
    out = v;
    return true;
}

// ------------------------------------------------------------- processes ---
// Reads a small file (from /proc) into buf, NUL-terminated. Returns its length, or -1.
inline long read_small_file(const char *path, char *buf, size_t size) {
    const int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return -1;
    const ssize_t n = read(fd, buf, size - 1);
    close(fd);
    if (n < 0) return -1;
    buf[n] = 0;
    return long(n);
}

// Does process `pid` exist? (Signal 0 only checks; EPERM would still mean it exists.)
inline bool process_exists(pid_t pid) { return pid > 0 && !(kill(pid, 0) == -1 && errno == ESRCH); }

// How long process `pid` has been running, in seconds: /proc/uptime minus its start time (field 22
// of /proc/<pid>/stat, in clock ticks since boot). -1 if it's gone.
inline double process_age(pid_t pid) {
    char path[48], stat_line[1024], uptime[96];
    snprintf(path, sizeof path, "/proc/%d/stat", int(pid));
    if (read_small_file(path, stat_line, sizeof stat_line) <= 0 ||
        read_small_file("/proc/uptime", uptime, sizeof uptime) <= 0)
        return -1;
    // Field 2 is the name in parentheses, which may hold spaces: count from the last ')'.
    const char *p = strrchr(stat_line, ')');
    if (!p) return -1;
    p++;
    for (int field = 3; field < 22; field++) {  // skip fields 3..21
        while (*p == ' ') p++;
        while (*p && *p != ' ') p++;
    }
    char *rest = nullptr;
    const unsigned long long start = strtoull(p, &rest, 10);
    const long ticks = sysconf(_SC_CLK_TCK);
    if (rest == p || ticks <= 0) return -1;
    return strtod(uptime, nullptr) - double(start) / double(ticks);
}

// SteamVR's processes, found by name (/proc/<pid>/comm).
struct SteamVrProcesses {
    std::vector<pid_t> servers;  // "vrserver" (normally just one)
    bool compositor = false;     // a "vrcompositor" exists
};

inline SteamVrProcesses find_steamvr() {
    SteamVrProcesses found;
    DIR *d = opendir("/proc");
    if (!d) return found;
    while (const dirent *e = readdir(d)) {
        if (e->d_name[0] < '1' || e->d_name[0] > '9') continue;  // only the numbered (process) entries
        char path[300], comm[32];
        snprintf(path, sizeof path, "/proc/%s/comm", e->d_name);
        if (read_small_file(path, comm, sizeof comm) <= 0) continue;
        if (strcmp(comm, "vrserver\n") == 0)
            found.servers.push_back(pid_t(atol(e->d_name)));
        else if (strcmp(comm, "vrcompositor\n") == 0)
            found.compositor = true;
    }
    closedir(d);
    return found;
}

// The youngest of `servers` (normally the only one), or 0; its age in *age.
inline pid_t newest_vrserver(const std::vector<pid_t> &servers, double *age = nullptr) {
    pid_t best = 0;
    double best_age = 0;
    for (const pid_t s : servers) {
        const double a = process_age(s);
        if (a >= 0 && (!best || a < best_age)) {
            best = s;
            best_age = a;
        }
    }
    if (age) *age = best_age;
    return best;
}

// ---------------------------------------------------------------- images ---
// Deckhand's pictures: raw RGBA after an 8-byte header (uint32 width, height; little endian).
struct Image {
    uint32_t w = 0, h = 0;
    std::vector<uint8_t> px;
};

// Loads `path` into `out` (left untouched if the file is missing, short or not a sane size).
inline bool load_rgba(const std::string &path, Image &out) {
    FILE *f = fopen(path.c_str(), "rb");
    if (!f) return false;
    uint8_t hdr[8];
    Image im;
    bool ok = fread(hdr, 1, sizeof hdr, f) == sizeof hdr;
    if (ok) {
        im.w = uint32_t(hdr[0]) | (uint32_t(hdr[1]) << 8) | (uint32_t(hdr[2]) << 16) | (uint32_t(hdr[3]) << 24);
        im.h = uint32_t(hdr[4]) | (uint32_t(hdr[5]) << 8) | (uint32_t(hdr[6]) << 16) | (uint32_t(hdr[7]) << 24);
        ok = im.w > 0 && im.h > 0 && im.w <= 4096 && im.h <= 4096;
    }
    if (ok) {
        im.px.resize(size_t(im.w) * im.h * 4);
        ok = fread(im.px.data(), 1, im.px.size(), f) == im.px.size();
    }
    fclose(f);
    if (ok) out = std::move(im);
    return ok;
}

// -------------------------------------------------------- the connection ---
constexpr double kSettleSec = 1.5;        // keep every upload this long before destroying / disconnecting
constexpr double kServerMinAgeSec = 15;   // the gate: SteamVR must have been running this long
constexpr double kAliveCheckSec = 2;      // how often a connected helper checks its vrserver still exists
constexpr double kGateRetrySec = 2;       // connect() looks at the gate at most this often
constexpr double kInitRetrySec = 10;      // ... and waits this long after a failed VR_Init / CreateOverlay

// An overlay helper's SteamVR connection: opened only through the gate, let go only by the settle rule.
struct OverlayConnection {
    vr::IVRSystem *sys = nullptr;
    vr::IVROverlay *ov = nullptr;  // null while not connected
    pid_t server = 0;              // the vrserver we're connected to
    pid_t quit_server = 0;         // a vrserver that sent us VREvent_Quit: never connect to it again
    double last_upload = 0;        // CLOCK_MONOTONIC time of our last SetOverlayRaw (0: none yet)
    double next_alive_check = 0;
    double next_try = 0;           // connect() doesn't look again before this

    bool connected() const { return ov != nullptr; }

    // The safe connect gate: open only when SteamVR looks settled. A vrserver has been running for at
    // least kServerMinAgeSec, vrcompositor runs too, and it isn't the server that told us it is quitting
    // (its process lingers for a while after VREvent_Quit). Returns that server's pid, or 0 (closed).
    pid_t gate_open() {
        const SteamVrProcesses procs = find_steamvr();
        bool quitting_one_there = false;
        for (const pid_t s : procs.servers)
            if (s == quit_server) quitting_one_there = true;
        if (!quitting_one_there) quit_server = 0;  // it's gone: forget it (pids get reused, eventually)
        if (quitting_one_there || !procs.compositor) return 0;
        double age = 0;
        const pid_t newest = newest_vrserver(procs.servers, &age);
        return newest && age >= kServerMinAgeSec ? newest : 0;
    }

    // Connects if the gate is open, and quietly does nothing while it's closed. It looks at most every
    // kGateRetrySec (kInitRetrySec after a failure), so callers can simply keep asking.
    bool connect() {
        if (ov) return true;
        const double t = mono_now();
        if (t < next_try) return false;
        next_try = t + kGateRetrySec;
        const pid_t pid = gate_open();
        if (!pid) return false;
        vr::EVRInitError err = vr::VRInitError_None;
        vr::IVRSystem *s = vr::VR_Init(&err, vr::VRApplication_Overlay);
        if (err != vr::VRInitError_None) {
            fprintf(stderr, "SteamVR: %s\n", vr::VR_GetVRInitErrorAsEnglishDescription(err));
            next_try = mono_now() + kInitRetrySec;
            return false;
        }
        vr::IVROverlay *o = s ? vr::VROverlay() : nullptr;
        if (!o) {
            fprintf(stderr, "SteamVR: no overlay interface\n");
            vr::VR_Shutdown();
            next_try = mono_now() + kInitRetrySec;
            return false;
        }
        sys = s;
        ov = o;
        server = pid;
        last_upload = 0;
        next_alive_check = mono_now() + kAliveCheckSec;
        return true;
    }

    // SetOverlayRaw, noting the time for the settle rule.
    bool upload(vr::VROverlayHandle_t h, Image &im) {
        const vr::EVROverlayError e = ov->SetOverlayRaw(h, im.px.data(), im.w, im.h, 4);
        last_upload = mono_now();  // the compositor reads it from now on
        if (e == vr::VROverlayError_None) return true;
        fprintf(stderr, "SetOverlayRaw: %s\n", ov->GetOverlayErrorNameFromEnum(e));
        return false;
    }

    // True once the server we're connected to has died without a VREvent_Quit (looked at every
    // kAliveCheckSec). Then call drop(), not close().
    bool server_died() {
        if (!ov) return false;
        const double t = mono_now();
        if (t < next_alive_check) return false;
        next_alive_check = t + kAliveCheckSec;
        return !process_exists(server);
    }

    // VREvent_Quit: SteamVR is shutting down. Acknowledge it, and keep the gate shut for this server.
    // Then close() straight away, with no other calls.
    void quit_received() {
        sys->AcknowledgeQuit_Exiting();
        quit_server = server;
    }

    // The normal close: hide, wait out the settle time, destroy, disconnect. Handles become invalid.
    void close(vr::VROverlayHandle_t *handles, size_t n) {
        if (!ov) return;
        for (size_t i = 0; i < n; i++)
            if (handles[i] != vr::k_ulOverlayHandleInvalid) ov->HideOverlay(handles[i]);
        settle();
        for (size_t i = 0; i < n; i++)
            if (handles[i] != vr::k_ulOverlayHandleInvalid) {
                ov->DestroyOverlay(handles[i]);
                handles[i] = vr::k_ulOverlayHandleInvalid;
            }
        disconnect();
    }

    // The server died without a Quit: no Hide/Destroy calls on the dead connection, just disconnect.
    void drop(vr::VROverlayHandle_t *handles, size_t n) {
        for (size_t i = 0; i < n; i++) handles[i] = vr::k_ulOverlayHandleInvalid;
        if (ov) disconnect();
    }

    // Waits (fully, signals or not) until kSettleSec after the last upload.
    void settle() const {
        if (last_upload > 0) sleep_full(last_upload + kSettleSec - mono_now());
    }

    // VR_Shutdown, after the settle wait: disconnecting lets go of our side of the uploads too.
    void disconnect() {
        settle();
        vr::VR_Shutdown();
        sys = nullptr;
        ov = nullptr;
        server = 0;
        last_upload = 0;
    }
};
