"""Sky Eye Android — webview bootstrap entry point.

Uses p4a's webview bootstrap (no Kivy/SDL2/OpenGL required).
Starts the Sky Eye HTTP server on the port expected by p4a's native
WebView loader.

Storage priority:
  1. USB OTG SD card  (removable volume via getExternalFilesDirs)
  2. Primary external storage  (/sdcard)
  3. Fallback constant path
"""

from __future__ import annotations

import os
import sys
import threading
import time

PORT = 5000

# ── path setup ────────────────────────────────────────────────────────────────
_HERE   = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

# ── Android detection ─────────────────────────────────────────────────────────
try:
    from jnius import autoclass                              # type: ignore
    from android.permissions import (                        # type: ignore
        request_permissions, Permission,
    )
    _PythonActivity = autoclass("org.kivy.android.PythonActivity")
    _Environment    = autoclass("android.os.Environment")
    _Build          = autoclass("android.os.Build")
    _Settings       = autoclass("android.provider.Settings")
    _Intent         = autoclass("android.content.Intent")
    _Uri            = autoclass("android.net.Uri")
    ANDROID = True
except Exception:
    ANDROID = False


# ── storage detection ─────────────────────────────────────────────────────────

def _free_gb(path: str) -> float:
    try:
        s = os.statvfs(path)
        return round(s.f_bavail * s.f_frsize / 1e9, 1)
    except Exception:
        return 0.0


def _find_all_storage() -> list:
    """Return Android storage options for the UI (internal + any OTG volumes)."""
    if not ANDROID:
        return []
    mounts = []
    # Primary internal/external storage
    try:
        ext = _Environment.getExternalStorageDirectory().getAbsolutePath()
        mounts.append({
            "path": os.path.join(ext, "sky_eye", "tiles"),
            "label": "Internal Storage",
            "free_gb": _free_gb(ext),
            "total_gb": 0.0,
            "kind": "internal",
        })
    except Exception:
        mounts.append({
            "path": "/sdcard/sky_eye/tiles",
            "label": "Internal Storage",
            "free_gb": 0.0,
            "total_gb": 0.0,
            "kind": "internal",
        })
    # OTG / secondary volumes
    try:
        activity = _PythonActivity.mActivity
        dirs = activity.getExternalFilesDirs(None)
        for i in range(1, len(dirs)):
            d = dirs[i]
            if d is None:
                continue
            try:
                writable = d.canWrite()
            except Exception:
                writable = False
            path = d.getAbsolutePath() if writable else None
            if not path:
                continue
            root = path.split("/Android/data/")[0] if "/Android/data/" in path else path
            tile_path = os.path.join(root, "sky_eye", "tiles")
            mounts.append({
                "path": tile_path,
                "label": f"OTG SD Card {i}" if i > 1 else "OTG SD Card",
                "free_gb": _free_gb(root),
                "total_gb": 0.0,
                "kind": "otg",
            })
            print(f"[sky_eye] OTG storage found: {tile_path}")
    except Exception as e:
        print(f"[sky_eye] OTG detection error: {e}")
    return mounts


def _find_tile_root() -> str:
    if not ANDROID:
        return os.path.join(os.path.expanduser("~"), "tiles", "sky_eye")
    # Prefer OTG; fall back to internal
    for m in _find_all_storage():
        if m["kind"] == "otg":
            return m["path"]
    try:
        ext = _Environment.getExternalStorageDirectory().getAbsolutePath()
        return os.path.join(ext, "sky_eye", "tiles")
    except Exception:
        pass
    return "/sdcard/sky_eye/tiles"


# ── permission handling ───────────────────────────────────────────────────────

def _request_all_storage_perms() -> None:
    if not ANDROID:
        return
    request_permissions([
        Permission.READ_EXTERNAL_STORAGE,
        Permission.WRITE_EXTERNAL_STORAGE,
    ])
    try:
        if _Build.VERSION.SDK_INT >= 30:
            if not _Environment.isExternalStorageManager():
                print("[sky_eye] Requesting MANAGE_EXTERNAL_STORAGE via Settings…")
                activity = _PythonActivity.mActivity
                intent = _Intent(_Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION)
                uri = _Uri.fromParts("package", activity.getPackageName(), None)
                intent.setData(uri)
                activity.startActivity(intent)
    except Exception as e:
        print(f"[sky_eye] MANAGE_EXTERNAL_STORAGE request failed: {e}")


# ── HTTP server ───────────────────────────────────────────────────────────────

def _start_server(tile_root: str, android_mounts: list) -> None:
    from http.server import ThreadingHTTPServer
    import sky_eye as se
    se.Handler.ssd_dir = se.Path(tile_root)
    if ANDROID:
        se.Handler.android_mounts = android_mounts
    server = ThreadingHTTPServer(("127.0.0.1", PORT), se.Handler)
    print(f"[sky_eye] server up → tiles at {tile_root}")
    server.serve_forever()


# ── entry point ───────────────────────────────────────────────────────────────

_request_all_storage_perms()
android_mounts = _find_all_storage()
tile_root = _find_tile_root()
print(f"[sky_eye] tile root: {tile_root}")

threading.Thread(target=_start_server, args=(tile_root, android_mounts), daemon=True).start()

if not ANDROID:
    import webbrowser
    webbrowser.open(f"http://127.0.0.1:{PORT}")

# Keep the main thread alive (server runs in daemon thread)
while True:
    time.sleep(60)
