# Installing Quak

You don't need a camera to try it: every command takes `--sim`, which uses a simulated camera.

## Linux

You need:

- Python 3.11 or newer
- For a real camera: a Bluetooth adapter and BlueZ (most desktop distributions have it), and NetworkManager with `nmcli` to join
  the camera's Wi-Fi
- FFmpeg, for the live preview in the desktop app and for `quak preview --play`, which also needs `ffplay`

```bash
pipx install 'quak-camera[gui]'     # or 'quak-camera' for just the command line
```

The package is called `quak-camera` because `quak` on PyPI is an unrelated project. The commands are `quak` and `quak-gui`.

Or download the `.tar.gz` from [Releases](https://github.com/ADCDS/quak/releases), unpack it and run `./quak`. It needs glibc 2.34 or
newer (Ubuntu 22.04, Debian 12), GTK 3 and libmpv: on Debian and Ubuntu, `sudo apt install libgtk-3-0 libmpv2`.

The first `quak-gui` downloads the Flet desktop client, about 45 MB, to `~/.flet`.

It helps to have a second Wi-Fi adapter, such as a cheap USB one, because the adapter joining the camera leaves whatever
network it was on. Choose it with `--wifi-adapter NAME` or `QUAK_WIFI_ADAPTER`. With only one adapter, Quak asks first.

## Windows (experimental)

This hasn't been run with a camera, or launched at all, so expect problems and please report them.

```powershell
py -m venv quak-env
quak-env\Scripts\activate
pip install "quak-camera[gui]"
```

Or download the `.zip` from [Releases](https://github.com/ADCDS/quak/releases), unpack it and run `quak.exe`. It isn't code-signed,
so Windows may warn about it.

## Android

Download the APK from [Releases](https://github.com/ADCDS/quak/releases) and install it. Android asks you to allow installing from
your browser or file manager. It needs Android 10 or newer, and asks for Bluetooth and Location: Android requires Location permission
for a Bluetooth scan.

To check an APK came from this project, compare the certificate's SHA-256 digest from `apksigner verify --print-certs` with this one:

```text
fec9c12dc325dd08272a9163c60e033519d74ebd98a31e133b7b145b259bd59b
```

The first time Quak joins the camera's Wi-Fi, Android asks you to confirm the connection.

Downloads stay inside the app until you tap one to share or save it. Uninstalling the app deletes any you haven't shared.

## Finding your camera

```bash
quak scan                   # lists cameras nearby, with their addresses
quak pair                   # pairs the one on its pairing screen
quak pair --address AA:BB:CC:DD:EE:FF    # or choose one
```

To open the pairing screen on the camera: **Preferences > Connections > Connect Device > GoPro Quik App**.

## Building the packages

Most people don't need to: packaged builds are on the [Releases](https://github.com/ADCDS/quak/releases) page, with a `SHA256SUMS` file
for checking downloads. To build your own:

```bash
git clone https://github.com/ADCDS/quak.git && cd quak
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[gui]'
tools/build_app.sh linux      # needs clang, cmake, ninja, pkg-config, GTK 3 headers, liblzma and libmpv
tools/build_app.sh windows    # on Windows, from Git Bash
tools/build_app.sh apk        # needs Java 17
```

Flet downloads Flutter and the Android SDK the first time. The output lands in `app/build/`.

Maintainers can also run the `build` workflow from the repository's Actions tab.

APKs signed with different keys can't update each other, so to switch between builds, uninstall the old one first. That removes
the app's pairing and its downloads.
