# How Quak is organised

One Python backend does the work. The command line and the app are thin layers over it, so both behave the same way.

```text
  quak (cli/)      quak-gui (gui/)      test suites (e2e/)
         \               |                   /
          \         service.py              /
           \             |                 /
            \------- flows.py -------------      recipes with several steps: arm a schedule, livestream
                         |
                    camera.py                    one method per camera operation
                         |
                    session.py                   one Bluetooth session: requests, replies, keep-alive
                         |
                    transport/                   Bluetooth: ble.py (Linux, Windows), android.py, sim.py
```

| Folder or file | What it holds |
|---|---|
| `src/quak/core/` | Pure logic, with no input or output: message framing, protobuf, settings tables, log redaction, errors |
| `src/quak/transport/` | Bluetooth links, plus the simulated camera (`sim.py`, `sim_http.py`) |
| `src/quak/net/` | Wi-Fi joins (`nmcli.py`, `netsh.py`, `android_wifi.py`, `wifi.py`), the camera's HTTP API (`http.py`), the test hotspot, video streams |
| `src/quak/session.py` | Reassembles messages, matches replies to requests, and keeps the connection alive |
| `src/quak/camera.py` | The camera's operations, as methods |
| `src/quak/flows.py` | Operations with several steps and their cleanup |
| `src/quak/guard.py` | Saves settings and restores them around tests |
| `src/quak/service.py` | `CameraService`, the one API that the app uses, and the state it watches |
| `src/quak/i18n.py`, `src/quak/locale/` | The app's translations. Only the app sets a language, so the command line stays in English |
| `src/quak/cli/` | The `quak` command |
| `src/quak/gui/` | The Flet app. `screens/` has one file per screen, and `kit.py` holds shared controls |
| `src/quak/e2e/` | The end-to-end suites, run against the simulator or a real camera |
| `extensions/quak_native/` | A Flet extension giving Python access to Bluetooth and Wi-Fi on Android |
| `app/` | The project `flet build` packages. `tools/build_app.sh` copies `src/quak` into it |
| `tools/` | Build scripts, and generators for the settings tables, icons and images |

## Three ideas that shape the code

**The simulator speaks the real protocol.** `transport/sim.py` receives the same bytes a camera would, so tests exercise everything
above it. What it assumes about the camera is listed in its `ASSUMPTIONS` table and marked `VERIFIED` once checked on hardware. See
[testing.md](testing.md).

**One choke point for logging.** Every message that gets logged goes through `core/redact.py`, so private data stays out of the
Diagnostics screen, exported logs and `quak -v`. See [safety.md](safety.md).

**Cleaning up is part of the operation.** Anything that changes a computer's network or a camera's state undoes it in a `finally`
block, and leaves enough behind to recover if the process gets killed.

## On Android

Python runs inside the Flet app, with Dart plugins behind it. `QuakBle` and `QuakWifi` are Flet services defined in
`extensions/quak_native`, and `transport/android.py` and `net/android_wifi.py` use them through the same interfaces as the desktop
code. The live preview goes through a small local relay (`UdpHttpRelay`), because the phone's video player wouldn't open the camera's
UDP stream directly.
