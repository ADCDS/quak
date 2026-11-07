#!/usr/bin/env bash
# Build a packaged Quak app: tools/build_app.sh apk|linux|windows [extra flet build args]
# (APK architecture: QUAK_APK_ARCH, default arm64-v8a; x86_64 for the emulator)
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
target=${1:?apk, linux or windows}; shift || true
rm -rf "$here/app/src/quak"
cp -r "$here/src/quak" "$here/app/src/quak"
find "$here/app/src/quak" -name __pycache__ -prune -exec rm -rf {} +
rm -rf "$here/app/src/assets" && cp -r "$here/src/quak/gui/assets" "$here/app/src/assets"
# flet build doesn't re-resolve Dart packages when only the extension's pubspec changes
ext_pubspec="$here/extensions/quak_native/src/flutter/quak_native/pubspec.yaml"
lock="$here/app/build/flutter/pubspec.lock"
if [ -f "$lock" ] && [ "$ext_pubspec" -nt "$lock" ]; then
  echo "extension dependencies changed: regenerating the Flutter project"
  rm -rf "$here/app/build/flutter"
fi
cd "$here/app"
# Android only installs an update over an older build if this number is higher: 1.2.3 becomes 10203
IFS=. read -r major minor patch < <(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$here/src/quak/__init__.py")
args=(--yes --build-number $((major * 10000 + minor * 100 + patch)))
if [ "$target" = apk ]; then
  args+=(--arch ${QUAK_APK_ARCH:-arm64-v8a} --android-permissions android.permission.BLUETOOTH_SCAN=True
         android.permission.BLUETOOTH_CONNECT=True android.permission.ACCESS_FINE_LOCATION=True
         android.permission.ACCESS_WIFI_STATE=True android.permission.CHANGE_WIFI_STATE=True
         android.permission.CHANGE_NETWORK_STATE=True android.permission.NEARBY_WIFI_DEVICES=True
         android.permission.INTERNET=True
         android.permission.WRITE_SETTINGS=False)  # the Wi-Fi plugin asks for it, and Quak never changes system settings
fi
flet=$here/.venv/bin/flet
[ -x "$flet" ] || flet=flet  # CI installs it without a virtual environment
exec "$flet" build "$target" "${args[@]}" "$@"
