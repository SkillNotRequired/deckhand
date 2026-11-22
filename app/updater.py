"""Updates for Deckhand: checks GitHub for a newer version and installs it in place.

The app's own folder is replaced file by file from the repository's main branch; your
settings, rooms, maps and timers live elsewhere (~/.config, ~/.local/share) and are
never touched. The native helpers are rebuilt afterwards (build.sh).

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
# Short install command: is.gd/deckhandvr redirects to this repo's install.sh (permanent is.gd link)
INSTALL_SHORT = "curl -L is.gd/deckhandvr|sh"
INSTALL_FULL = f"curl -fsSL https://raw.githubusercontent.com/{REPO}/main/install.sh | bash"
BRANCH = "main"
VERSION_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/VERSION"
TARBALL_URL = f"https://codeload.github.com/{REPO}/tar.gz/refs/heads/{BRANCH}"
KEEP = {"setup.log", "last-run.log"}   # local files an update never overwrites or removes
# Files of the old flat layout (1.0.4 and earlier), removed after moving to app/ + helpers/
OBSOLETE = ["frame_settings.py", "ceiling.py", "house_map.py", "kitchen_timer.py", "power.py", "reminders.py",
            "rooms.py", "wifi_map.py", "steam_shortcut.py", "updater.py", "build.sh", "fanmode.sh",
            "timer_overlay", "chaperone_tool", "space_tool", "ceiling_overlay", "src", "__pycache__"]
OLD_LOGS = ["setup.log", "last-run.log", "power.log", "power.log.old"]
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
    except (OSError, ValueError, KeyError):
        _latest_sha = None
        return _get(f"{VERSION_URL}?t={int(time.time())}", timeout=timeout).decode().strip()


def check():
    """(local, latest, newer?) — raises OSError when GitHub can't be reached."""
    here, there = local_version(), latest_version()
    return here, there, _key(there) > _key(here)


def install(log=print):
    """Downloads the newest version (the commit check() found, else the main branch), copies it over
    this folder, rebuilds the helpers. Returns the new version. Raises on any failure before files
    are replaced."""
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
        if not os.path.isfile(os.path.join(src, "app", "deckhand.py")) or not os.path.isfile(os.path.join(src, "VERSION")):
            raise RuntimeError("the download doesn't look like Deckhand; nothing changed")
        for root, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            rel = os.path.relpath(root, src)
            dest_dir = os.path.normpath(os.path.join(ROOT, rel))
            os.makedirs(dest_dir, exist_ok=True)
            for name in files:
                if rel == "." and name in KEEP:
                    continue
                s, d = os.path.join(root, name), os.path.join(dest_dir, name)
                tmpd = d + ".new"
                shutil.copy2(s, tmpd)
                os.replace(tmpd, d)  # a running program keeps its old file
        for script in ("launch.sh", "install.sh", "uninstall.sh", "setup.sh", "helpers/build.sh",
                       "helpers/fanmode.sh", "app/deckhand.py"):
            p = os.path.join(ROOT, script)
            if os.path.exists(p):
                os.chmod(p, 0o755)
    log("files updated, building helpers…")
    cleanup()
    r = subprocess.run(["bash", os.path.join(ROOT, "helpers", "build.sh")], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("helpers didn't build: " + (r.stderr or r.stdout).strip()[-300:])
    return local_version()


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


def migrate_units():
    """Points Deckhand's background services at this copy of the app (after an update or a move):
    the auto shut-off service, the ceiling indicator and any pending reminders."""
    import ceiling
    import paths
    import power
    changed = []
    for unit_path, wanted, fix in ((power.UNIT_PATH, power.UNIT, power.ensure_service),
                                   (ceiling.UNIT_PATH, ceiling.UNIT_TEXT, lambda: ceiling.apply_service(ceiling.load()))):
        try:
            with open(unit_path, encoding="utf-8") as f:
                current = f.read()
        except OSError:
            continue
        if current != wanted:
            fix()
            if unit_path == ceiling.UNIT_PATH and subprocess.run(
                    ["systemctl", "--user", "is-active", "--quiet", ceiling.UNIT]).returncode == 0:
                subprocess.run(["systemctl", "--user", "restart", ceiling.UNIT], capture_output=True)
            changed.append(os.path.basename(unit_path))
    unit_dir = os.path.expanduser("~/.config/systemd/user")
    try:
        names = [n for n in os.listdir(unit_dir) if n.startswith("frame-reminder-")]
    except OSError:
        names = []
    for name in names:  # reminders: swap the helper / script paths for this copy's
        p = os.path.join(unit_dir, name)
        with open(p, encoding="utf-8") as f:
            text = f.read()
        new = re.sub(r"\S*/timer_overlay\b", paths.tool("timer_overlay"), text)
        new = re.sub(r"\S*/reminders\.py\b", os.path.join(HERE, "reminders.py"), new)
        if new != text:
            with open(p, "w", encoding="utf-8") as f:
                f.write(new)
            changed.append(name)
    if names:
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
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
