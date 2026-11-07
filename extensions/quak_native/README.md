# quak-native

The Flet extension Quak uses on Android, with two services for Python:

- `QuakBle`: Bluetooth LE scan, connect, bond, GATT read and write, and notifications, built on the
  [universal_ble](https://pub.dev/packages/universal_ble) Flutter plugin (BSD-3-Clause).
- `QuakWifi`: joining the camera's Wi-Fi for this app only, built on the
  [wifi_iot](https://pub.dev/packages/wifi_iot) Flutter plugin (MIT). Android 10 or newer.

It isn't published on PyPI and is only meant for Quak's packaged Android build: `app/pyproject.toml` maps it to this folder.
