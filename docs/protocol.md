# HERO11 protocol notes

What we learned getting a Linux PC (BlueZ 5.82, a USB BT 5.4 dongle) to control a HERO11
Black (firmware H22.01.02.20.00) over BLE. Addresses below are placeholders: camera
`AA:BB:CC:DD:EE:FF`, PC adapter `11:22:33:44:55:66`.

The reference is GoPro's Open GoPro spec: <https://gopro.github.io/OpenGoPro/docs/ble/>.
Its per-camera support tables are rendered client-side. The data sits in a JS chunk under
`/OpenGoPro/assets/js/`, so grep the chunks for `"HERO11 Black"`.

## Connecting: the camera only accepts known centrals

The camera silently ignores connection requests from any central it hasn't bonded with,
unless it is on its pairing screen.

- **Advertisement:** the manufacturer data (company 0x02F2) shows it: status byte bit 2 = pairing mode.

  | byte | meaning |
  |---|---|
  | 0 | schema version (2) |
  | 1 | status: bit0 awake, bit1 Wi-Fi AP on, **bit2 pairing mode**, bit3 central role, bit4 new media |
  | 2 | model id (58 = HERO11 Black, 60 = HERO11 Black Mini) |
  | 3 | capabilities |
  | 5..10 | the camera's BD address, reversed |
  | 11 | media offload status |

- **Failure signature** (btmon):

  ```
  < LE Extended Create Connection      peer: Random AA:BB:CC:DD:EE:FF
  > LE Enhanced Connection Complete    Status: Success, interval 50 ms
  > Disconnect Complete                Reason: Connection Failed to be Established (0x3e)   t = +250 ms
  ```

  "Connection Complete: Success" only means CONNECT_IND was sent. The peripheral never answers, and after 6 connection events (6 × 50 ms) the link is dropped. bluetoothd reports `le-connection-abort-by-local`. The same behaviour is reported in OpenGoPro issues #456 and #476.
- **Fix, done once:**
  1. on the camera, open Preferences → Connections → Connect Device → (phone app);
  2. connect, then pair (Just Works: camera is NoInputNoOutput, legacy pairing, bonding);
  3. send GoPro's *pairing finished* message: protobuf `RequestPairingFinish{result=SUCCESS, phoneName}`, feature 0x03 / action 0x01 on `b5f90091`, answered `03 81 08 01` on `b5f90092`.

  Later connections work without pairing mode.
- **Ruled out:** random-static address handling, connection parameters, extended vs legacy create-connection, LL privacy.
- **Watch out:** a busy controller. BR/EDR audio streaming on the same dongle made LE scans sparse, and the dongle once stopped answering `LE Set Extended Scan Parameters` (USB reset).

## Settings need Pro controls

In **Easy** controls (setting 175 = 0) the camera offers no allowed values for scheduled capture (168), video duration (156) or multi-shot duration (157), and rejects writes with status 1. Taking camera control (status 114 → 2) does not help. Switching to **Pro** (175 = 1) does.

## Scheduled capture (setting 168)

- **Value:** `hour<<8 | minute<<2 | is_24h<<1 | enabled`. For example 12:34 → `0x0C8B`, written as `06 A8 04 00 00 0C 8B`, reply `02 a8 00`. This matches the SDK's `ScheduledCaptureParser` and what the camera accepts. GoPro's docs table (minute bits 2–8, hour 9–13) contradicts its own example.
- **Status:** 108 = armed; 107 = the preset that will be used.
- **Preset:** the schedule runs **the preset that is active when you arm it**. Arming while the camera sat on Photo set 107 = 0x00010000, a Photo preset.
- **Duration:** without a duration (156/157), the recording starts and runs until stopped. The manual's tip is to set Duration.
- **Clock:** the schedule fires on the **camera clock**. The official app sets the clock on every connect; BLE command 0x0F sets local date/time with tz offset and DST.
- **Auto Power Off:** the camera stays on after the capture until Auto Power Off (59) runs out.
- **Sleep and power-off:** a schedule survives both BLE sleep (0x05) and the power button: the camera switched itself on and recorded. It is one-shot: after firing, 168 is off and status 108 drops to 0.
- **Durations:** setting 156 goes from 15 s to 3 h, plus No Limit. There is no 5 s option.

## Seen on real hardware (firmware 2.20)

- **Presets:** video presets have ids `0x0009xxxx`, unlike the documented `0x0000xxxx`.
- **Preset loads** are refused for a few seconds after leaving Slo-Mo.
- **Setting 31** isn't documented. It can differ on the Burst preset and can't be written.
- **Adverts:** when not pairing, a camera can advertise as rarely as every 10–20 s.
- **After a power loss on USB:** once, GATT services never resolved until the camera was restarted.
- **Media API:** `path=100GOPRO/X.MP4` needs a literal slash; `%2F` returns HTTP 400.
- **Media timestamps:** `cre` is camera-local wall time encoded as an epoch, and can be up to about 20 s
  before the shutter press.
- **A stream left running:** if a client dies during preview, the camera keeps status 32 at 1 and answers 409 to
  `stream/start`, even after a restart of the app. `stream/stop` clears it.
- **Preview and recording:** the preview stream stops when a recording starts, can't be restarted during it,
  and stays off afterwards until `/gopro/camera/stream/start` is called again.
- **Livestream lens:** setting 43 (Webcam Digital Lenses) is also the livestream lens, and only a stream that
  actually starts changes it.
- **Livestream after camera Wi-Fi:** the camera can ignore `SetLiveStreamMode` (it stays IDLE with no error)
  right after its own Wi-Fi was turned off. A BLE sleep and wake clears that, and so does a restart.
- **Joining a network:** the camera can associate and finish the handshake but never ask for an address. See
  the known issues in [testing.md](testing.md).

## Message cheat sheet

| What | Characteristic | Bytes |
|---|---|---|
| Get date/time | 0072 | `01 0e` → `0b 0e 00 08 YY YY MM DD hh mm ss ..` |
| Get statuses | 0076 | `NN 13 id id ..` → TLV list |
| Get settings / allowed values | 0076 | `NN 12 id ..` / `NN 32 id ..` |
| Write setting | 0074 | `LL id len value..` → `02 id status` (0 ok, 1 error, 2 invalid) |
| Camera control | 0072 | `04 f1 69 08 VV` (2 = external, 0 = idle) |
| Sleep | 0072 | `01 05` |
| Hardware info | 0072 | `01 3c` |
