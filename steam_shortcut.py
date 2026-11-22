#!/usr/bin/env python3
"""Install (or update) Deckhand's launchers, live.

- Steam library shortcut with icon and library artwork (tile, header, hero,
  logo from assets/steam/), so it shows up like any other app in VR.
- App menu entry (~/.local/share/applications/deckhand.desktop) with icon.

SteamOS runs Steam with its DevTools port on 127.0.0.1:8080. This talks to the
SharedJSContext page over a minimal stdlib WebSocket and calls Steam's own
SteamClient.Apps functions, so Steam does not need to restart.

    python3 steam_shortcut.py           add or update
    python3 steam_shortcut.py remove    remove both launchers
"""
import base64
import json
import os
import socket
import struct
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "Deckhand"
OLD_NAMES = ["Frame Settings"]  # earlier name: renamed in place, not duplicated
EXE = os.path.join(HERE, "launch.sh")
ART = os.path.join(HERE, "assets", "steam")
ICON = os.path.join(ART, "icon.png")
# Steam's library asset types for SetCustomArtworkForApp
ARTWORK = {"portrait.png": 0, "hero.png": 1, "logo.png": 2, "capsule.png": 3}
APP_ID = "deckhand"  # window class set by frame_settings.py
OLD_APP_IDS = ["frame-settings"]
DESKTOP_FILE = os.path.expanduser(f"~/.local/share/applications/{APP_ID}.desktop")
THEME_ICON = os.path.expanduser(f"~/.local/share/icons/hicolor/256x256/apps/{APP_ID}.png")
DEVTOOLS = "http://127.0.0.1:8080/json"


def ws_connect(url):
    rest = url.split("://", 1)[1]
    hostport, path = rest.split("/", 1)
    host, port = hostport.split(":")
    s = socket.create_connection((host, int(port)), timeout=15)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = s.recv(4096)
        if not chunk:
            raise ConnectionError("DevTools closed the connection during the handshake")
        resp += chunk
    if b" 101 " not in resp.split(b"\r\n", 1)[0]:
        raise ConnectionError(resp.split(b"\r\n", 1)[0].decode(errors="replace"))
    return s


def ws_send(s, text):
    data = text.encode()
    head = bytearray([0x81])
    n = len(data)
    if n < 126:
        head.append(0x80 | n)
    elif n < 65536:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", n)
    mask = os.urandom(4)
    s.sendall(bytes(head) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


def _recv_exact(s, n):
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return buf


def ws_recv(s):
    message = b""
    while True:
        b1, b2 = _recv_exact(s, 2)
        opcode, n = b1 & 0x0F, b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", _recv_exact(s, 2))[0]
        elif n == 127:
            n = struct.unpack(">Q", _recv_exact(s, 8))[0]
        if b2 & 0x80:
            mask = _recv_exact(s, 4)
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(_recv_exact(s, n)))
        else:
            payload = _recv_exact(s, n)
        if opcode == 0x8:
            raise ConnectionError("DevTools closed the connection")
        if opcode in (0x9, 0xA):
            continue
        message += payload
        if b1 & 0x80:
            return message.decode()


def evaluate(expression):
    targets = json.load(urllib.request.urlopen(DEVTOOLS, timeout=5))
    target = next((t for t in targets if t.get("title") == "SharedJSContext"), None)
    if not target:
        raise SystemExit("Steam's SharedJSContext page was not found (is Steam running?)")
    s = ws_connect(target["webSocketDebuggerUrl"])
    try:
        ws_send(s, json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {
            "expression": expression, "awaitPromise": True, "returnByValue": True}}))
        while True:
            msg = json.loads(ws_recv(s))
            if msg.get("id") == 1:
                break
    finally:
        s.close()
    res = msg.get("result", {})
    if "exceptionDetails" in res:
        raise SystemExit("Steam JS error: " + json.dumps(res["exceptionDetails"])[:500])
    return res.get("result", {}).get("value")


FIND = """
const findShortcut = (...names) => {
  try {
    return (window.appStore?.allApps || []).find(
      a => a.app_type === 1073741824 && names.includes(a.display_name)) || null;
  } catch (e) { return null; }
};
"""


def add():
    for path in (EXE, os.path.join(HERE, "frame_settings.py")):
        if not os.path.isfile(path):
            raise SystemExit(f"missing {path}")
    args = json.dumps({"name": NAME, "old": OLD_NAMES, "exe": f'"{EXE}"', "dir": f'"{HERE}"',
                       "icon": ICON if os.path.isfile(ICON) else ""})
    js = f"""
(async () => {{
  {FIND}
  const a = {args};
  const A = SteamClient.Apps;
  const found = findShortcut(a.name, ...a.old);
  const appid = found ? found.appid : await A.AddShortcut(a.name, a.exe, a.dir, "");
  A.SetShortcutName(appid, a.name);
  A.SetShortcutExe(appid, a.exe);
  A.SetShortcutStartDir(appid, a.dir);
  A.SetShortcutLaunchOptions(appid, "");
  if (a.icon && A.SetShortcutIcon) A.SetShortcutIcon(appid, a.icon);
  await new Promise(r => setTimeout(r, 1500));
  const now = findShortcut(a.name);
  return JSON.stringify({{appid, existed: !!found, listed: !!now,
                          name: now?.display_name ?? null}});
}})()
"""
    install_desktop_entry()  # works without Steam's DevTools
    try:
        result = json.loads(evaluate(js))
    except (OSError, ValueError) as e:
        raise SystemExit(f"Steam library shortcut not added ({e}). Add it by hand: in Desktop mode open Steam, "
                         f"Games > Add a Non-Steam Game > Browse > {EXE}")
    print("shortcut:", result)
    set_artwork(result["appid"])


def set_artwork(appid):
    for fname, asset_type in ARTWORK.items():
        path = os.path.join(ART, fname)
        if not os.path.isfile(path):
            print(f"artwork: {fname} missing, skipped")
            continue
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        js = (f"SteamClient.Apps.SetCustomArtworkForApp({int(appid)}, {json.dumps(b64)}, 'png', {asset_type})"
              ".then(() => 'ok', e => 'error: ' + e)")
        print(f"artwork: {fname} -> {evaluate(js)}")


def install_desktop_entry():
    """App menu entry + the icon in the user's icon theme. StartupWMClass matches
    the window class the app sets (deckhand), so taskbars show the icon."""
    for old in OLD_APP_IDS:  # entries from the earlier name
        for path in (os.path.expanduser(f"~/.local/share/applications/{old}.desktop"),
                     os.path.expanduser(f"~/.local/share/icons/hicolor/256x256/apps/{old}.png")):
            if os.path.exists(path):
                os.remove(path)
    os.makedirs(os.path.dirname(THEME_ICON), exist_ok=True)
    with open(ICON, "rb") as src, open(THEME_ICON, "wb") as dst:
        dst.write(src.read())
    os.makedirs(os.path.dirname(DESKTOP_FILE), exist_ok=True)
    with open(DESKTOP_FILE, "w", encoding="utf-8") as f:
        f.write("[Desktop Entry]\nType=Application\n"
                f"Name={NAME}\nComment=Fan, power, timers, rooms, Wi-Fi maps and recline for the Steam Frame\n"
                f"Exec={EXE}\nPath={HERE}\nIcon={APP_ID}\nStartupWMClass={APP_ID}\n"
                "Categories=Utility;\nTerminal=false\n")
    os.chmod(DESKTOP_FILE, 0o755)
    print(f"app menu entry: {DESKTOP_FILE}\nicon: {THEME_ICON}")


def remove():
    js = f"""
(async () => {{
  {FIND}
  const found = findShortcut({json.dumps(NAME)}, ...{json.dumps(OLD_NAMES)});
  if (!found) return "not in library";
  SteamClient.Apps.RemoveShortcut(found.appid);
  return "removed " + found.appid;
}})()
"""
    print(evaluate(js))
    for path in (DESKTOP_FILE, THEME_ICON):
        if os.path.exists(path):
            os.remove(path)
            print(f"removed {path}")


if __name__ == "__main__":
    remove() if sys.argv[1:] == ["remove"] else add()
