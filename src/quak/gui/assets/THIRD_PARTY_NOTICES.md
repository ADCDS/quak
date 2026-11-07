# Third-party notices

Quak's own source code is under the MIT License (see `LICENSE`). The packaged apps also contain the software below, each
under its own licence. Links point to where you can get the source code and the full licence text.

## mpv and FFmpeg (LGPL)

The Android and Windows packages contain `libmpv`, which plays the camera's live preview, and FFmpeg inside it. The Linux
package uses the `libmpv` installed on your system.

| Component | Version | Licence | Source |
|---|---|---|---|
| mpv, built without GPL parts (`-Dgpl=false`) | from the builds below | LGPL-2.1-or-later | <https://github.com/mpv-player/mpv> |
| FFmpeg | n6.0 | LGPL-3.0-or-later | <https://ffmpeg.org/download.html> |
| Android build of the two (`media_kit_libs_android_video` 1.3.8) | | as above | <https://github.com/media-kit/libmpv-android-video-build> |
| Windows build of the two (`media_kit_libs_windows_video` 1.0.11) | | as above | <https://github.com/media-kit/libmpv-win32-video-build> |

Quak loads these as separate shared libraries (`libmpv.so`, `libmpv-2.dll`), so you can replace them with your own builds.
On Android that means rebuilding the app from this repository, as described in `docs/install.md`.

The full licence texts are in `src/quak/gui/assets/licenses/`, and they ship inside the apps: `LGPL-2.1.txt`, `LGPL-3.0.txt`
and `GPL-3.0.txt`, which LGPL-3.0 builds on.

## Fonts (SIL Open Font License 1.1)

Inter, Space Grotesk and JetBrains Mono. Their licences are in `src/quak/gui/assets/fonts/`.

## Other components

| Component | Version | Licence | Where |
|---|---|---|---|
| Flutter | 3.47.9 | BSD-3-Clause | packaged apps |
| Flet and flet-video | 1.1.0 | Apache-2.0 | packaged apps, `quak-gui` |
| Python | 3.14 | PSF-2.0 | packaged apps |
| media_kit | 1.2.6 | MIT | packaged apps |
| media_kit_video | 2.0.1 | MIT | packaged apps |
| universal_ble | 3.1.0 | BSD-3-Clause | Android |
| wifi_iot | 0.4.0 | MIT | Android |
| bleak | 1.x or 0.22 | MIT | CLI and desktop |
| dbus-fast | 2.x | MIT | Linux |
| certifi | | MPL-2.0 | through Flet |
| typing_extensions | | PSF-2.0 | through Flet |

Flutter apps also bundle the notices of every Dart package they use, in their `NOTICES` asset.

## GoPro's Open GoPro SDK

Quak doesn't use this SDK at run time, but `tools/gen_tables.py` generates Quak's settings tables from it, and its tests
compare Quak's messages with it. Its notice:

```text
The OpenGoPro Python SDK is licensed under the MIT License

Copyright © 2021-2024 GoPro, Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
