[app]

title = Sky Eye
package.name = skyeye
package.domain = org.privatenemo

# Source: the parent directory contains sky_eye.py + static/
source.dir = ..
source.include_exts = py,png,jpg,jpeg,html,js,css,json
source.include_patterns = static/*,android/*

# p4a's webview bootstrap only looks for main.py/main.pyc at the private root.
entrypoint = main.py

version = 2.0.5

# Webview bootstrap — no Kivy/SDL2/OpenGL needed, just Python + native WebView
p4a.bootstrap = webview
requirements = hostpython3==3.13.3,python3==3.13.3,android,pillow,openssl

# ── Permissions ───────────────────────────────────────────────────────────────
# IMPORTANT: spell these exactly — wrong casing silently breaks them (learned
# from nRF Connect APK rebuild where ACCESS_FINE_LOCATION was mangled).
#
# MANAGE_EXTERNAL_STORAGE is required on Android 11+ (API 30+) to read/write
# USB OTG SD card paths outside the app sandbox. The runtime flow in main.py
# sends the user to Settings if this hasn't been granted yet.
android.permissions = INTERNET,READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE,MANAGE_EXTERNAL_STORAGE

# android.features unsupported by this p4a version; USB host declared via manifest instead
# android.features = android.hardware.usb.host

# requestLegacyExternalStorage keeps Android 10 (API 29) from sandboxing paths
# before MANAGE_EXTERNAL_STORAGE was available.
# android.extra_manifest_application_arguments = android:requestLegacyExternalStorage="true"

# ── API targets ───────────────────────────────────────────────────────────────
android.api = 33
android.minapi = 26
android.ndk = 25b
android.sdk = 33
android.archs = arm64-v8a,armeabi-v7a

# Landscape suits the map UI
orientation = landscape
fullscreen = 0

android.release_artifact = apk

[buildozer]
log_level = 2
warn_on_root = 1
