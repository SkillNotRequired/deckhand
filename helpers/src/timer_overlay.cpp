// timer_overlay: the in-headset part of Deckhand's kitchen timer and reminders.
//
//   timer_overlay <end_epoch_seconds> <image_dir> [popup_seconds]
//   timer_overlay --live <file.rgba> [parent_pid]
//   timer_overlay --minimap <file.rgba> [parent_pid]
//
// Countdown: sleeps until `popup_seconds` (default 60) before the end, then shows a countdown
// pinned to the top-centre of your view (a SteamVR overlay locked to the headset) from
// <image_dir>/NN.rgba (NN = seconds left). At zero it sounds the alarm, shows <image_dir>/done.rgba
// for 30 s and rings twice more. Reminders use popup_seconds 0: only done.rgba and the alarm, at the
// due time. If SteamVR isn't up then (a reminder missed while the Frame was off fires right after it
// boots), a reminder waits up to 15 minutes for SteamVR so it is seen and not just heard, and rings
// without it after that. SIGTERM (Cancel in the app) hides the popup and exits.
//
// --live: shows <file> a little above the centre of your view and re-uploads it whenever it is
// replaced (Deckhand's Wi-Fi readout); --minimap: the same as a see-through panel in the middle of
// the view (the house walk's mini-map). Both run until SIGTERM, or until process <parent_pid>
// (Deckhand) has gone.
//
// Pictures are raw RGBA (see load_rgba in common.h) uploaded with SetOverlayRaw: the picture is
// swapped in place, with no decoding and no blank frame between seconds (SetOverlayFromFile
// flickered and dropped seconds). Each process uses its own overlay key, so two timers or
// reminders can be up at once.
//
// Light on battery: while waiting it sleeps in long chunks (signals still wake it), and it only
// connects to SteamVR for the popup itself, through the safe connect gate (common.h): it never
// starts SteamVR. Without SteamVR the alarm still sounds.
#include "common.h"

#include <algorithm>
#include <climits>
#include <cmath>
#include <spawn.h>
#include <string>
#include <sys/stat.h>
#include <sys/wait.h>
#include <vector>

extern char **environ;

// Head-locked placement: a panel kAhead metres in front of the eyes, raised by up_tan * kAhead.
constexpr float kAhead = 1.1f;
constexpr float kTimerUpTan = 0.2493f;  // tan(14 deg): timers and reminders, high in the view
constexpr float kLiveUpTan = 0.0875f;   // tan(5 deg): the live readout, under any timer popup
constexpr float kPanelWidth = 0.42f;    // metres (about 22 degrees)
constexpr float kMinimapWidth = 0.32f;  // the mini-map sits right in the middle (it's see-through)
constexpr double kDoneSec = 30;               // "done" stays up this long
constexpr double kReminderWaitSec = 15 * 60;  // a due reminder waits this long for SteamVR

// ------------------------------------------------------------------ alarm ---
// paplay processes still playing: reaped as they finish, never waited for.
static std::vector<pid_t> g_players;

// Starts the alarm sound and returns at once (a stuck sound server must not hold up the popup).
static void play_alarm() {
    static const char *const kSounds[] = {"/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga",
                                          "/usr/share/sounds/ocean/stereo/alarm-clock-elapsed.oga",
                                          "/usr/share/sounds/freedesktop/stereo/complete.oga"};
    for (const char *sound : kSounds) {
        if (access(sound, R_OK) != 0) continue;
        posix_spawnattr_t attr;
        posix_spawnattr_init(&attr);
        sigset_t restore;  // we ignore SIGPIPE (helper_init); paplay gets the default back
        sigemptyset(&restore);
        sigaddset(&restore, SIGPIPE);
        posix_spawnattr_setsigdefault(&attr, &restore);
        posix_spawnattr_setflags(&attr, POSIX_SPAWN_SETSIGDEF);
        posix_spawn_file_actions_t io;
        posix_spawn_file_actions_init(&io);
        posix_spawn_file_actions_addopen(&io, STDOUT_FILENO, "/dev/null", O_WRONLY, 0);
        char *const args[] = {const_cast<char *>("paplay"), const_cast<char *>(sound), nullptr};
        pid_t pid = 0;
        const int rc = posix_spawnp(&pid, "paplay", &io, &attr, args, environ);
        if (rc == 0)
            g_players.push_back(pid);
        else
            fprintf(stderr, "paplay: %s\n", strerror(rc));
        posix_spawn_file_actions_destroy(&io);
        posix_spawnattr_destroy(&attr);
        return;
    }
    fprintf(stderr, "no alarm sound found\n");
}

static void reap_players() {
    g_players.erase(std::remove_if(g_players.begin(), g_players.end(),
                                   [](pid_t pid) { return waitpid(pid, nullptr, WNOHANG) != 0; }),
                    g_players.end());
}

// ------------------------------------------------------------------ popup ---
// One head-locked overlay. It connects (through the gate) only when there is something to show.
struct Popup {
    OverlayConnection link;
    std::string key;  // per process: a second timer or reminder must not fail with KeyInUse
    float up_tan, width;
    vr::VROverlayHandle_t h = vr::k_ulOverlayHandleInvalid;
    bool shown = false;

    Popup(const char *key_prefix, float up_tangent, float width_m)
        : key(key_prefix + std::to_string(getpid())), up_tan(up_tangent), width(width_m) {}

    bool is_open() const { return h != vr::k_ulOverlayHandleInvalid; }

    // Connects and creates the overlay (still hidden). False while the gate is closed or it failed.
    bool open() {
        if (is_open()) return true;
        if (!link.connect()) return false;
        const vr::EVROverlayError e = link.ov->CreateOverlay(key.c_str(), "Deckhand", &h);
        if (e != vr::VROverlayError_None) {
            fprintf(stderr, "CreateOverlay %s: %s\n", key.c_str(), link.ov->GetOverlayErrorNameFromEnum(e));
            h = vr::k_ulOverlayHandleInvalid;
            link.close(&h, 1);  // nothing uploaded: just disconnects
            link.next_try = mono_now() + kInitRetrySec;
            return false;
        }
        // Locked to the headset: kAhead in front, raised by up_tan, `width` wide, facing the eyes.
        const vr::HmdMatrix34_t m = {{{1, 0, 0, 0}, {0, 1, 0, kAhead * up_tan}, {0, 0, 1, -kAhead}}};
        link.ov->SetOverlayWidthInMeters(h, width);
        link.ov->SetOverlayTransformTrackedDeviceRelative(h, vr::k_unTrackedDeviceIndex_Hmd, &m);
        link.ov->SetOverlaySortOrder(h, 200);
        link.ov->SetOverlayAlpha(h, 0.95f);
        shown = false;
        return true;
    }

    // Swaps in `im`; the popup is shown once, after its first picture is in (no empty frame).
    bool show(Image &im) {
        if (!open() || !link.upload(h, im)) return false;
        if (!shown) {
            link.ov->ShowOverlay(h);
            shown = true;
        }
        return true;
    }

    // SteamVR's events, and the liveness check. False when the popup isn't open (any more).
    bool poll() {
        if (!is_open()) return false;
        if (link.server_died()) {
            fprintf(stderr, "SteamVR went away without quitting\n");
            link.drop(&h, 1);
            shown = false;
            return false;
        }
        vr::VREvent_t ev;
        while (link.sys->PollNextEvent(&ev, sizeof ev))
            if (ev.eventType == vr::VREvent_Quit) {  // close once, now, and keep away from this server
                link.quit_received();
                close();
                return false;
            }
        return true;
    }

    // Hide, settle, destroy, disconnect (common.h).
    void close() {
        link.close(&h, 1);
        shown = false;
    }
};

// -------------------------------------------------------------- countdown ---
static int countdown(double end, const std::string &dir, int popup) {
    Popup p("framesettings.timer.", kTimerUpTan, kPanelWidth);
    for (;;) {
        // Sleep until the popup is due, in chunks: a signal ends it, and a changed clock is noticed.
        while (!g_quit && wall_now() < end - popup) nap(std::min(30.0, end - popup - wall_now()));
        if (g_quit || popup == 0) break;

        // The last `popup` seconds: NN.rgba each second.
        int last = -1;
        bool clock_went_back = false;
        while (!g_quit) {
            const double left = end - wall_now();
            if (left <= 0) break;
            if (left > popup + 2) {  // the clock was set back: take the popup down and wait again
                clock_went_back = true;
                break;
            }
            const int secs = int(std::ceil(left));
            p.poll();  // events first: never upload to a SteamVR that has just said it's quitting
            if (secs != last && p.open()) {
                char name[32];
                snprintf(name, sizeof name, "/%02d.rgba", std::min(secs, 60));
                Image im;
                if (load_rgba(dir + name, im))
                    p.show(im);
                else
                    fprintf(stderr, "can't read %s%s\n", dir.c_str(), name);
                last = secs;
            }
            // Sleep to just past the next whole second, so every second is shown on time.
            const double to_go = end - wall_now();
            const double frac = to_go > 0 ? to_go - std::floor(to_go) : 0;
            nap(std::max(frac, 0.02) + 0.005);
        }
        if (!clock_went_back) break;
        p.close();
    }
    if (g_quit) {
        p.close();
        return 0;
    }

    // Time's up.
    Image done;
    const bool have_done = load_rgba(dir + "/done.rgba", done);
    if (!have_done) fprintf(stderr, "can't read %s/done.rgba\n", dir.c_str());
    if (popup == 0 && have_done && !p.link.gate_open()) {
        // A reminder, and SteamVR isn't (settled and) up: e.g. the Frame just booted and a reminder
        // it missed while off fired. Give SteamVR time, so the reminder is seen; then ring anyway.
        fprintf(stderr, "waiting for SteamVR to show the reminder\n");
        const double give_up = mono_now() + kReminderWaitSec;
        while (nap(2) && mono_now() < give_up && !p.link.gate_open()) {
        }
        if (g_quit) return 0;
    }
    // The alarm first: the sound never waits on SteamVR. Then "done", retried (every ~2 s, through
    // the gate) until SteamVR takes it, for the whole 30 s.
    play_alarm();
    const double t0 = mono_now();
    int rings = 1;
    bool done_up = false;  // done.rgba is on screen (the popup may still show the last second)
    while (!g_quit && mono_now() - t0 < kDoneSec) {
        if (rings < 3 && mono_now() - t0 >= 4.0 * rings - 0.05) {  // twice more: at 4 s and 8 s
            play_alarm();
            rings++;
        }
        if (!p.poll()) done_up = false;  // not open (any more): show it again if SteamVR comes back
        if (have_done && !done_up) done_up = p.show(done);
        reap_players();
        nap(0.5);
    }
    p.close();
    reap_players();
    return 0;
}

// -------------------------------------------------------- live / minimap ---
static int live(const char *path, bool minimap, pid_t parent) {
    Popup p(minimap ? "framesettings.minimap." : "framesettings.live.", minimap ? 0.0f : kLiveUpTan,
            minimap ? kMinimapWidth : kPanelWidth);
    Image im;              // the newest picture from `path`
    bool pending = false;  // ... not on screen yet
    ino_t seen_ino = 0;    // which version of the file `im` is (Deckhand replaces it, os.replace)
    timespec seen_mtime{0, 0};
    double next_parent_check = 0;
    if (parent > 0 && !process_exists(parent)) {
        // Deckhand passes its own PID right before starting us, so it can't have gone yet: it is in
        // another PID namespace (a sandboxing launcher). Don't watch a number that means nothing here.
        fprintf(stderr, "process %d isn't visible from here: not watching it\n", int(parent));
        parent = 0;
    }
    while (!g_quit) {
        // Deckhand gone (it crashed or was killed without stopping us): don't leave its picture in view.
        const double now = mono_now();
        if (parent > 0 && now >= next_parent_check) {
            next_parent_check = now + 1;
            if (!process_exists(parent)) {
                fprintf(stderr, "process %d has gone: closing\n", int(parent));
                break;
            }
        }
        // Events first (never upload to a SteamVR that is quitting). If SteamVR quit or died, the
        // popup is gone: put the picture back once SteamVR is back.
        if (!p.poll() && !im.px.empty()) pending = true;
        struct stat st;
        if (stat(path, &st) == 0 && (st.st_ino != seen_ino || st.st_mtim.tv_sec != seen_mtime.tv_sec ||
                                     st.st_mtim.tv_nsec != seen_mtime.tv_nsec)) {
            seen_ino = st.st_ino;
            seen_mtime = st.st_mtim;
            if (load_rgba(path, im)) pending = true;
        }
        if (pending && p.show(im)) pending = false;
        nap(0.2);
    }
    p.close();
    return 0;
}

// ------------------------------------------------------------------- main ---
static int usage(const char *me) {
    fprintf(stderr,
            "usage: %s <end_epoch> <image_dir> [popup_seconds]\n"
            "       %s --live <file.rgba> [parent_pid]\n"
            "       %s --minimap <file.rgba> [parent_pid]\n",
            me, me, me);
    return 2;
}

int main(int argc, char **argv) {
    helper_init();
    if (argc >= 2 && (strcmp(argv[1], "--live") == 0 || strcmp(argv[1], "--minimap") == 0)) {
        long parent = 0;
        if (argc < 3 || argc > 4 || (argc == 4 && !parse_long(argv[3], 1, INT_MAX, parent))) return usage(argv[0]);
        return live(argv[2], strcmp(argv[1], "--minimap") == 0, pid_t(parent));
    }
    double end = 0;
    long popup = 60;
    if (argc < 3 || argc > 4 || !parse_double(argv[1], end) || end <= 0 ||
        (argc == 4 && !parse_long(argv[3], 0, 86400, popup)))
        return usage(argv[0]);
    return countdown(end, argv[2], int(popup));
}
