# Changelog

## Unreleased

- SSH password login alongside keys: `quak ssh profile set --auth password`, `--password-stdin` for
  `test`/`exec`, and a Key/Password choice with a prompt in the app's SSH tab. Passwords are never stored.
- Signed driver packs can be installed from a local folder or a hash-pinned https/file URL, with zip
  traversal/symlink/size checks and signature verification before any camera write.
- The Lab Toolkit card can install, load, unload and remove the bundled kernel pack from the app.
- Blank-password SSH (`--blank-password`; leave the app's password field empty) for gopro-root's default
  `AUTH=blank` cameras. Encrypted OpenSSH keys imported into the app are not yet reliable; use a password,
  blank password, or an unencrypted key.
- SSH key rows can install a key on the camera or forget it locally; the phone Test button now uses the
  embedded Dart SSH client when the system `ssh` binary is absent. Optional signed driver packs can be
  installed from a local path with `quak lab toolkit install-pack`.
- Lab device panels: USB microphone recording and parallel camera+mic capture, Ethernet/USB-Wi-Fi
  interface configuration, framebuffer and serial-port discovery. Lab actions now also work on password
  profiles, prompting once per action.
- **Camera Lab** (advanced, Linux, experimental): for a HERO11 that already has its own SSH login, a new `quak ssh` and `quak lab`.
  `quak ssh` arms the camera's existing SSH service with one documented request, pins the host key, and manages SSH keys and
  profiles; `quak lab` switches the USB-C port to USB host mode (reversibly, in RAM), enumerates USB devices, installs and loads a
  bundled, kernel-matched toolkit, mounts storage read-only, and reads system status. Quak installs nothing on the camera, and
  every change is explicit, confirmed and undoable where the hardware allows. See
  [the Camera Lab guide](https://github.com/ADCDS/quak/blob/main/docs/camera-lab.md).

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
