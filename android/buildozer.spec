[app]

title = Sky Eye
package.name = skyeye
package.domain = org.privatenemo

# Source: the parent directory contains sky_eye.py + static/
source.dir = ..
source.include_exts = py,png,jpg,jpeg,kv,atlas,html,js,css,json
source.include_patterns = static/*,android/*

# Entry point is android/main.py
entrypoint = android/main.py

version = 2.0.0

# Pure-Python deps only (no Flask needed — Sky Eye uses stdlib HTTP server)
requirements = python3,kivy==2.3.0,pillow,android

android.permissions = INTERNET,WRITE_EXTERNAL_STORAGE,READ_EXTERNAL_STORAGE,\
    ACCESS_NETWORK_STATE

# Target modern Android; minimum API 26 (Android 8.0) for WebView stability
android.api = 33
android.minapi = 26
android.ndk = 25b
android.sdk = 33
android.archs = arm64-v8a,armeabi-v7a

# Keep landscape — better for the map UI
orientation = landscape
fullscreen = 0

# Presplash / icon (drop replacements here)
# presplash.filename = %(source.dir)s/android/presplash.png
# icon.filename      = %(source.dir)s/android/icon.png

android.release_artifact = apk

[buildozer]
log_level = 2
warn_on_root = 1
