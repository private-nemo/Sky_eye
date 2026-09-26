"""Sky Eye Android — Kivy shell that boots the Flask-less HTTP server and shows a WebView."""

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
    from jnius import autoclass                             # type: ignore
    _PythonActivity  = autoclass("org.kivy.android.PythonActivity")
    _WebView         = autoclass("android.webkit.WebView")
    _WebViewClient   = autoclass("android.webkit.WebViewClient")
    _WebSettings     = autoclass("android.webkit.WebSettings")
    ANDROID = True
except Exception:
    ANDROID = False

PORT = 8091

# ── path setup ────────────────────────────────────────────────────────────────
# sky_eye.py lives one directory up from this file in the source tree.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)


def _tile_root() -> str:
    """Return a writable tile directory appropriate for the platform."""
    if ANDROID:
        # Kivy sets ANDROID_APP_PATH at runtime; fall back to internal storage.
        base = os.environ.get("ANDROID_APP_PATH", "/sdcard/sky_eye")
        return os.path.join(base, "tiles")
    return os.path.join(os.path.expanduser("~"), "tiles", "sky_eye")


def _start_server() -> None:
    """Boot the Sky Eye HTTP server in a daemon thread."""
    from http.server import ThreadingHTTPServer
    # Import Handler after path is set up
    import sky_eye as se                       # noqa: PLC0415
    se.Handler.ssd_dir = se.Path(_tile_root())
    server = ThreadingHTTPServer(("127.0.0.1", PORT), se.Handler)
    print(f"[sky_eye] server up on port {PORT}")
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

        # Start HTTP server
        t = threading.Thread(target=_start_server, daemon=True)
        t.start()

        # Give the server a moment, then open the UI
        Clock.schedule_once(self._open_ui, 1.8)
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
    def _load_webview(url: str) -> None:
        """Replace the activity content view with a full-screen WebView."""
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
