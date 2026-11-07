# Changelog

## 0.2.0

Quak now speaks your language. Tested in these languages on the Linux desktop app and one Android phone, but not on Windows.

- The app and its main docs are now in Português (Brasil), Español, Français, Deutsch, Русский, 简体中文 and 日本語. The app follows your
  system's language, or you can choose one in More. These are machine translations, and
  [corrections are welcome](https://github.com/ADCDS/quak/blob/main/CONTRIBUTING.md#translations)
- Fixed: the Play button in the desktop app crashed after downloading a clip

The APK updates an installed 0.1.0 in place, keeping your pairing and downloads.

## 0.1.0

The first public version. It's early, and has been tested on one HERO11 Black (firmware 2.20) and one Android phone.

Install it with `pipx install 'quak-camera[gui]'`, or download an app below. The
[install guide](https://github.com/ADCDS/quak/blob/main/docs/install.md) has the details for each system.

- A command line and an app for Linux, Windows and Android, on one Python backend
- Pairing, status, every HERO11 Black setting, presets, recording and HiLights
- Scheduled capture, with clock sync and duration
- Live preview, media browsing, download and delete over the camera's Wi-Fi
- A simulated camera that speaks the real protocol, for trying Quak and testing it without a camera

Experimental: livestream ([known issue](https://github.com/ADCDS/quak/blob/main/docs/troubleshooting.md#known-issues)) and Windows, which has never been run with a camera.
