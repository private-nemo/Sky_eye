# Sky Eye — Android Build

Packages the Sky Eye tile generator as a native Android APK using
[Buildozer](https://buildozer.readthedocs.io/) + [Kivy](https://kivy.org/).

The app boots the existing Python HTTP server in a background thread and
displays it in a full-screen Android WebView — no rewrite required.

## Prerequisites (Linux build machine)

```bash
sudo apt install -y python3-pip git zip unzip openjdk-17-jdk \
    autoconf libtool pkg-config zlib1g-dev libncurses5-dev \
    libncursesw5-dev libtinfo5 cmake libffi-dev libssl-dev

pip3 install --user buildozer cython
```

## Build

```bash
cd sky_eye/android       # this directory
buildozer android debug
```

The first build downloads the Android SDK/NDK and compiles all
dependencies — expect 20–40 minutes. Subsequent builds are much faster.

Output APK: `bin/skyeye-2.0.0-arm64-v8a_armeabi-v7a-debug.apk`

## Install on device

```bash
buildozer android deploy run   # requires USB debugging + adb
# or sideload the APK manually
```

## Tile storage

On device, tiles are saved to:
```
/sdcard/sky_eye/tiles/
```

For Garmin JNX output, copy the `.jnx` file from there to
`Garmin/BirdsEye/` on the GPS unit's SD card.

## CI / GitHub Actions

See `.github/workflows/build.yml` in the repo root for an automated
cloud build that produces a downloadable APK artifact.
