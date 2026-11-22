"""Updates for Deckhand: checks GitHub for a newer version and installs it in place.

The app's own folder is replaced file by file from the repository's main branch; your
settings, rooms, maps and timers live elsewhere (~/.config, ~/.local/share) and are
never touched. The native helpers are rebuilt afterwards (build.sh). VERSION is written
last, once everything worked: an update that fails part-way is still offered, so it can
simply be tried again.

    python3 updater.py           check, and update if there's a newer version
    python3 updater.py check     only check
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)  # the install folder (app/ is inside it)
REPO = "SkillNotRequired/deckhand"
# Short install command: is.gd/deckhandvr redirects to this repo's install.sh (permanent is.gd link).
# https: nobody on the network can swap the script; -f: an error page is never run as a script.
INSTALL_SHORT = "curl -fsSL https://is.gd/deckhandvr|sh"
BRANCH = "main"
VERSION_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/VERSION"
TARBALL_URL = f"https://codeload.github.com/{REPO}/tar.gz/refs/heads/{BRANCH}"
# Files of the old flat layout (1.0.4 and earlier), removed after moving to app/ + helpers/
OBSOLETE = ["frame_settings.py", "ceiling.py", "house_map.py", "kitchen_timer.py", "power.py", "reminders.py",
            "rooms.py", "wifi_map.py", "steam_shortcut.py", "updater.py", "build.sh", "fanmode.sh",
            "timer_overlay", "chaperone_tool", "space_tool", "ceiling_overlay", "src", "__pycache__"]
OLD_LOGS = ["setup.log", "last-run.log", "power.log", "power.log.old"]
SCRIPTS = ["launch.sh", "install.sh", "uninstall.sh", "setup.sh", "helpers/build.sh", "helpers/fanmode.sh",
           "app/deckhand.py"]
SERVICES = ["frame-autooff.service", "frame-ceiling.service"]  # always-on jobs that run this folder's code
BUILD_TIMEOUT = 900  # the helpers build at the lowest priority; this only catches a hang
UA = {"User-Agent": "deckhand-updater"}


def local_version():
    try:
        with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "0"


def _key(v):
    return tuple(int(p) if p.isdigit() else 0 for p in v.strip().split("."))


_latest_sha = None  # the commit the last check() found; install() downloads exactly that one


def _get(url, accept=None, timeout=10):
    headers = dict(UA, **({"Accept": accept} if accept else {}))
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
        return r.read()


def latest_version(timeout=10):
    """Newest version on GitHub. Asks the API for the latest commit (no caching delay right after a
    release); falls back to the raw file (cached by GitHub for up to ~5 minutes)."""
    global _latest_sha
    try:
        commit = json.loads(_get(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}",
                                 "application/vnd.github+json", timeout))
        sha = commit["sha"]
        version = _get(f"https://api.github.com/repos/{REPO}/contents/VERSION?ref={sha}",
                       "application/vnd.github.raw", timeout).decode().strip()
        _latest_sha = sha
        return version
    except (OSError, ValueError, KeyError, TypeError):
        _latest_sha = None
        return _get(f"{VERSION_URL}?t={int(time.time())}", timeout=timeout).decode().strip()


def check():
    """(local, latest, newer?) — raises OSError when GitHub can't be reached."""
    here, there = local_version(), latest_version()
    return here, there, _key(there) > _key(here)


def _write_file(path, data):
    """Replaces a file whole (written beside it, then renamed in: never half a file)."""
    tmp = os.path.join(os.path.dirname(path), f".{os.path.basename(path)}.new")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, path)


def _copy_files(src):
    """Copies the download over this folder, every file but VERSION (install() writes that last).
    Each file is replaced whole: a running program keeps its old copy."""
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        rel = os.path.relpath(root, src)
        dest_dir = os.path.normpath(os.path.join(ROOT, rel))
        os.makedirs(dest_dir, exist_ok=True)
        for name in files:
            if rel == "." and name == "VERSION":
                continue
            d = os.path.join(dest_dir, name)
            shutil.copy2(os.path.join(root, name), d + ".new")
            os.replace(d + ".new", d)
    for script in SCRIPTS:
        p = os.path.join(ROOT, script)
        if os.path.exists(p):
            os.chmod(p, 0o755)


def _restart_services():
    """The always-on services run this folder's code: restart the ones that are running."""
    try:
        subprocess.run(["systemctl", "--user", "try-restart", *SERVICES], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass


def install(log=print):
    """Downloads the newest version (the commit check() found, else the main branch) and installs it
    over this folder: the files, the old layout's cleanup and the helper build first, VERSION last.
    Returns the new version. Raises with a message for the app on failure; nothing is changed if the
    download fails, and after a failed build the new files stay but VERSION doesn't change."""
    url = f"https://codeload.github.com/{REPO}/tar.gz/{_latest_sha}" if _latest_sha else TARBALL_URL
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        blob = r.read()
    log(f"downloaded {len(blob) // 1024} KB")
    with tempfile.TemporaryDirectory(prefix="deckhand-") as tmp:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            tar.extractall(tmp, filter="data")  # refuses absolute paths, links out of the folder, devices
        tops = os.listdir(tmp)
        src = os.path.join(tmp, tops[0]) if len(tops) == 1 else tmp
        try:
            with open(os.path.join(src, "VERSION"), encoding="utf-8") as f:
                version_text = f.read()
        except (OSError, ValueError):
            version_text = ""
        new_version = version_text.strip()
        if not os.path.isfile(os.path.join(src, "app", "deckhand.py")) or not re.fullmatch(r"\d[\w.+-]{0,31}",
                                                                                         new_version):
            raise RuntimeError("the download doesn't look like Deckhand; nothing changed")
        _copy_files(src)
    log("files updated, building helpers…")
    try:
        cleanup(ROOT)
    except OSError as e:
        raise RuntimeError(f"Updated files but couldn't tidy up the old ones: {e}") from e
    failed = ""
    try:
        r = subprocess.run(["bash", os.path.join(ROOT, "helpers", "build.sh")], capture_output=True, text=True,
                           timeout=BUILD_TIMEOUT)
        if r.returncode != 0:
            failed = (r.stderr or r.stdout).strip()[-300:] or f"exit code {r.returncode}"
    except (OSError, subprocess.SubprocessError) as e:
        failed = str(e)
    if failed:  # the new files stay; the old VERSION keeps the update on offer
        raise RuntimeError(f"Updated files but the helpers didn't build: {failed}")
    _write_file(os.path.join(ROOT, "VERSION"), version_text)
    _restart_services()
    return new_version


def cleanup(root=ROOT):
    """Removes the old flat layout's files (they live in app/ and helpers/ now); old logs go to logs/."""
    if not os.path.isfile(os.path.join(root, "app", "deckhand.py")):
        return  # never clean a folder that doesn't have the new layout in place
    for name in OBSOLETE:
        p = os.path.join(root, name)
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.lexists(p):
            os.remove(p)
    os.makedirs(os.path.join(root, "logs"), exist_ok=True)
    for name in OLD_LOGS:
        p = os.path.join(root, name)
        if os.path.isfile(p):
            os.replace(p, os.path.join(root, "logs", name))


# A reminder's ExecStart line, or what an earlier version of migrate_units() left of it (the bare
# path: it swallowed "ExecStart="), and its clean-up line. Anchored at the line start so the key
# stays; the replacement is a function, so nothing in a path is read as a regex escape.
_TOOL_LINE = re.compile(r"^(?:ExecStart=)?/(?:\S*/)?timer_overlay(?=\s|$)", re.M)
_DONE_LINE = re.compile(r"^(ExecStopPost=\S+\s+)/(?:\S*/)?reminders\.py(?=\s|$)", re.M)


def fix_reminder_unit(text, tool, script):
    """A reminder unit's text pointing at helper `tool` and clean-up script `script`. Also repairs
    units an earlier version broke. Running it again changes nothing."""
    text = _TOOL_LINE.sub(lambda _m: "ExecStart=" + tool, text)
    return _DONE_LINE.sub(lambda m: m.group(1) + script, text)


def _systemctl(*args):
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(args, 1, "", str(e))


def migrate_units():
    """Points Deckhand's background services at this copy of the app (after an update or a move):
    the auto shut-off service, the ceiling indicator and any pending reminders."""
    import ceiling
    import paths
    import power
    changed = []
    for unit_path, wanted, fix, unit in ((power.UNIT_PATH, power.UNIT, power.ensure_service, None),
                                         (ceiling.UNIT_PATH, ceiling.UNIT_TEXT,
                                          lambda: ceiling.apply_service(ceiling.load()), ceiling.UNIT)):
        try:
            with open(unit_path, encoding="utf-8") as f:
                current = f.read()
        except OSError:
            continue
        if current == wanted:
            continue
        try:
            fix()
            if unit:  # a running overlay keeps the old program until restarted
                _systemctl("try-restart", unit)
            changed.append(os.path.basename(unit_path))
        except Exception as e:  # noqa: BLE001 (the other units still get their turn)
            print(f"{os.path.basename(unit_path)}: {e}", file=sys.stderr)

    unit_dir = os.path.expanduser("~/.config/systemd/user")
    tool, script = paths.tool("timer_overlay"), os.path.join(HERE, "reminders.py")
    try:
        names = sorted(n for n in os.listdir(unit_dir) if n.startswith("frame-reminder-") and n.endswith(".service"))
    except OSError:
        names = []
    fixed = []
    for name in names:
        p = os.path.join(unit_dir, name)
        try:
            with open(p, encoding="utf-8") as f:
                text = f.read()
            new = fix_reminder_unit(text, tool, script)
            if new != text:
                _write_file(p, new)
                fixed.append(name)
        except (OSError, ValueError) as e:
            print(f"{name}: {e}", file=sys.stderr)
    if fixed:
        _systemctl("daemon-reload")
        for name in fixed:
            # A reminder whose unit was broken failed when it came due: start its timer again. With
            # Persistent=true a missed reminder shows right away; one that already rang stays done.
            timer = name[:-len(".service")] + ".timer"
            if os.path.isfile(os.path.join(unit_dir, timer)) and _systemctl("is-active", "--quiet", timer).returncode:
                _systemctl("reset-failed", timer)
                _systemctl("start", timer)
        changed += fixed
    return changed


if __name__ == "__main__":
    if sys.argv[1:] == ["cleanup"]:
        cleanup()
        sys.exit(0)
    if sys.argv[1:] == ["migrate"]:
        print("updated:", ", ".join(migrate_units()) or "nothing to change")
        sys.exit(0)
    here, there, newer = check()
    print(f"installed {here}, latest {there}")
    if newer and sys.argv[1:] != ["check"]:
        print("updated to", install())
