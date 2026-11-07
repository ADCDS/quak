# Testing

For people working on Quak. The [README](../README.md#what-has-been-tested) has the summary of what's been tested on hardware.

| Level | Command | Needs |
|---|---|---|
| Unit tests | `python -m unittest discover -s tests -t .` | nothing |
| End to end on the simulator | `python -m quak.e2e --sim` and `python -m quak.e2e.gui --sim` | ffmpeg; Chrome for the GUI suite |
| End to end on a camera | `python -m quak.e2e` and `python -m quak.e2e.gui` | a paired HERO11 Black, and the Linux Wi-Fi setup below |

CI runs the first two levels on every push, and the unit tests on Linux and Windows.

Useful flags: `--only G03,G04,G11` runs some steps, and `python -m quak.e2e --human` includes the scenario that needs someone to press
the camera's power button. GUI step G07 needs G04's clip, and G11 needs G03's list of what was already on the card.

## The simulator

`src/quak/transport/sim.py` speaks the real byte protocol. Its `ASSUMPTIONS` table lists what it believes about the camera, marked
`VERIFIED` once checked on hardware. When the simulator and a camera disagree, the camera wins: correct the simulator.

## What a real-camera run does to the camera

- **Settings:** noted first and restored afterwards. Undocumented setting 31 can't be written back, so it's reported and doesn't
  fail the run.
- **Media:** only clips the run recorded are deleted. Both suites refuse to delete anything without a complete list of what was on
  the card beforehand.
- **Networks:** the camera only joins a throwaway hotspot on the PC, whose credentials stay in `~/.config/quak/hotspot.json`. Other
  saved networks are refused. The camera has no API to forget a network, so it keeps that entry until Reset Connections.
- **Never used:** formatting a card, deleting everything, or a factory reset.

The real-camera GUI suite sets `QUAK_DEV=1`, which shows the self-test and Live's test mode. The GUI suite finds things by their
English labels, so it also sets `QUAK_LANG=en`.

## Linux Wi-Fi setup for real-camera runs

- Use a USB Wi-Fi adapter that isn't your main connection, and name it with `QUAK_WIFI_ADAPTER` if you have several. The test hotspot
  has to run on 2.4 GHz if the adapter only supports that.
- Pair the phone before the PC touches the camera, because a connection from the PC closes the camera's pairing screen.
- Check `journalctl -k` for `tx timeout` before blaming Quak for a failed run: see [troubleshooting](troubleshooting.md#bluetooth-errors).
- The CLI suite makes a new Bluetooth connection for every command. If that crashes your adapter, slow it down with `--rest 20`.

## Results on real hardware

HERO11 Black, firmware 2.20, Linux with BlueZ, November 2026.

**CLI suite.** All 19 scenarios (S00–S18) have passed, though not all in a single run: S15 needs a person, so it ran separately.

On the code in 0.1.0, 15 passed (S00–S11, S14, S16, S18). The test PC's USB Bluetooth dongle crashed about 40 times during those runs, so
many passed only on a later attempt, with `--rest 20`, running one scenario at a time:

- S13 (a schedule firing with the camera on) never finished, because the dongle kept dropping the connection. The camera did record on
  time each time it was armed, and S14 and GUI step G08 passed.
- S12 (removing the run's clips) never finished either. Deleting clips worked, and the card was checked by hand against its original list.
- S15 (needs a person) and S17 (livestream, a [known issue](troubleshooting.md#known-issues)) weren't run again.

**GUI suite.** G01–G08, G10, G11 and the restore all pass. G09 (livestream) is a [known issue](troubleshooting.md#known-issues).

**Android.** Tested by hand on one phone: scanning, pairing, reconnecting, clock, recording, recording during preview, joining the
camera's Wi-Fi, media list, thumbnails, info, download, opening the share sheet for a download, delete, live preview, changing mode
during preview, and the Back button. Not tested: scheduled capture, settings, HiLights, livestream, and the self-test.

**Translations.** Every screen was checked by eye on the simulator at phone size in all seven translated languages, and in German and
Japanese at desktop size. The language picker and following the system's language were tested in the Linux desktop app. They haven't been
tried on Android or Windows, or reviewed by native speakers. The camera's menu names in Spanish and German come from copies of GoPro's HERO11
manual. In the other languages they come from GoPro's support pages, so the app shows the English next to them.

**Windows.** Never launched. CI runs the unit tests on Windows, and the `netsh` code is only unit-tested with invented output.

**Packaged builds.** The `build` workflow runs by hand, from the Actions tab or with `gh workflow run build.yml`, and on a version
tag:

| Build | Checked |
|---|---|
| Linux | Launched, and walked every screen on the simulator |
| Windows | Built, and its contents inspected. Never launched |
| Android | Built and signed, and its contents, permissions and signature inspected. The phone tests above used a debug-signed build from earlier the same day |

## Images

`tools/make_images.py` makes `docs/images` from the simulator. Never take them from a real camera, because its screens show the serial
and nearby network names. `tools/make_icons.py` makes the logo and app icons from one drawing.
