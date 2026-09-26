"""Sky Eye Android — Kivy shell that boots the HTTP server and shows a WebView.

Storage priority:
  1. USB OTG SD card  (secondary external storage, detected via getExternalFilesDirs)
  2. Primary external storage  (/sdcard)
  3. App-internal fallback
"""

from __future__ import annotations

import os
import sys
import threading

from kivy.app import App
from kivy.clock import Clock
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.label import Label

# ── Android detection ─────────────────────────────────────────────────────────
try:
    from android.runnable import run_on_ui_thread          # type: ignore
    from android.permissions import (                      # type: ignore
        request_permissions, check_permission, Permission,
    )
    from jnius import autoclass                             # type: ignore
    _PythonActivity  = autoclass("org.kivy.android.PythonActivity")
    _WebView         = autoclass("android.webkit.WebView")
    _WebViewClient   = autoclass("android.webkit.WebViewClient")
    _Environment     = autoclass("android.os.Environment")
    _Build           = autoclass("android.os.Build")
    _Settings        = autoclass("android.provider.Settings")
    _Intent          = autoclass("android.content.Intent")
    _Uri             = autoclass("android.net.Uri")
    ANDROID = True
except Exception:
    ANDROID = False

PORT = 8091

# ── path setup ────────────────────────────────────────────────────────────────
_HERE   = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)


# ── storage detection ─────────────────────────────────────────────────────────

def _find_tile_root() -> str:
    """
    Return the best available writable path for tile storage.

    On Android we walk getExternalFilesDirs(null): index 0 is the built-in
    /sdcard, index 1+ are removable volumes — the first of those is the
    USB OTG SD card when one is mounted.
    """
    if not ANDROID:
        return os.path.join(os.path.expanduser("~"), "tiles", "sky_eye")

    try:
        activity = _PythonActivity.mActivity
        dirs = activity.getExternalFilesDirs(None)
        # dirs[0] = primary (internal SD), dirs[1+] = removable (OTG / microSD slot)
        for i in range(1, len(dirs)):          # prefer removable first
            d = dirs[i]
            if d is not None and d.canWrite():
                path = d.getAbsolutePath()
                # Walk up to the volume root rather than the app-private subdir
                # .../Android/data/org.privatenemo.skyeye/files → volume root
                root = path.split("/Android/data/")[0] if "/Android/data/" in path else path
                tile_path = os.path.join(root, "sky_eye", "tiles")
                print(f"[sky_eye] USB OTG storage found: {tile_path}")
                return tile_path
    except Exception as e:
        print(f"[sky_eye] OTG detection error: {e}")

    # Fallback: primary external (/sdcard)
    try:
        ext = _Environment.getExternalStorageDirectory().getAbsolutePath()
        return os.path.join(ext, "sky_eye", "tiles")
    except Exception:
        pass

    return "/sdcard/sky_eye/tiles"


# ── permission handling ───────────────────────────────────────────────────────

def _request_all_storage_perms() -> None:
    """
    Request storage permissions at runtime.

    Android 11+ (API 30+): MANAGE_EXTERNAL_STORAGE is required for full
    removable-storage access (USB OTG). We send the user to the system
    Settings page if it hasn't been granted yet — same approach used in
    the nRF Connect APK rebuild.

    Android ≤ 10: READ/WRITE_EXTERNAL_STORAGE is sufficient; we also set
    requestLegacyExternalStorage in the manifest (buildozer.spec) for API 29.
    """
    if not ANDROID:
        return

    # Always request classic storage permissions (needed for API ≤ 32)
    request_permissions([
        Permission.READ_EXTERNAL_STORAGE,
        Permission.WRITE_EXTERNAL_STORAGE,
    ])

    # On API 30+ also request MANAGE_EXTERNAL_STORAGE via Settings intent
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


# ── Flask-less HTTP server ────────────────────────────────────────────────────

def _start_server(tile_root: str) -> None:
    from http.server import ThreadingHTTPServer
    import sky_eye as se                       # noqa: PLC0415
    se.Handler.ssd_dir = se.Path(tile_root)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), se.Handler)
    print(f"[sky_eye] server up → tiles at {tile_root}")
    server.serve_forever()


# ── Kivy app ──────────────────────────────────────────────────────────────────

class SkyEyeAndroid(App):
    title = "Sky Eye"

    def build(self):
        self._layout = FloatLayout()
        self._status = Label(
            text="Starting Sky Eye…",
            color=(0.55, 0.65, 1, 1),
            font_size="16sp",
        )
        self._layout.add_widget(self._status)

        # Request storage permissions immediately on launch
        _request_all_storage_perms()

        # Determine tile root (OTG SD card preferred)
        tile_root = _find_tile_root()
        self._status.text = f"Tiles → {tile_root}\nStarting server…"

        # Boot the HTTP server in a daemon thread
        threading.Thread(target=_start_server, args=(tile_root,), daemon=True).start()

        # Open the UI after the server has had time to bind
        Clock.schedule_once(self._open_ui, 2.0)
        return self._layout

    def _open_ui(self, _dt):
        url = f"http://127.0.0.1:{PORT}"
        if ANDROID:
            self._load_webview(url)
        else:
            import webbrowser
            webbrowser.open(url)
            self._status.text = f"Sky Eye running at {url}"

    @staticmethod
    @run_on_ui_thread
    def _load_webview(url: str) -> None:
        try:
            activity = _PythonActivity.mActivity
            wv = _WebView(activity)
            settings = wv.getSettings()
            settings.setJavaScriptEnabled(True)
            settings.setDomStorageEnabled(True)
            settings.setLoadWithOverviewMode(True)
            settings.setUseWideViewPort(True)
            settings.setBuiltInZoomControls(True)
            settings.setDisplayZoomControls(False)
            wv.setWebViewClient(_WebViewClient())
            wv.loadUrl(url)
            activity.setContentView(wv)
        except Exception as exc:
            print(f"[sky_eye] WebView error: {exc}")


if __name__ == "__main__":
    SkyEyeAndroid().run()
