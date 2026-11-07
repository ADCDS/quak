# Troubleshooting

## Pairing and connecting

**"No camera is on its pairing screen".** A camera ignores devices it doesn't know unless this screen is open: **Preferences >
Connections > Connect Device > GoPro Quik App**. Open it, then run `quak pair` again.

**Pairing the phone and then the computer fails.** Connecting from another device closes the camera's pairing screen. Pair one
device at a time, and reopen the screen before pairing the next.

**The camera is "not seen within 45 s".** Check that it's on and nearby. When it isn't pairing, a HERO11 can advertise as rarely as
every 10–20 seconds, and an asleep camera may take longer. Try again, or press a button on the camera to wake it.

**It connects, but never finishes setting up.** Once, after a camera lost power while on USB, it stopped offering its Bluetooth
services until it was restarted. Turn the camera off and on.

**The camera refuses to record or change settings, with no reason given.** It may be stuck in Turbo Transfer mode, which blocks
recording, settings and preview. Quak turns this on for downloads, so a connection that drops during one can leave it on. The app clears
it when it next connects, and the command line says so in its error: run `quak turbo off`.

## Bluetooth errors

**"Bluetooth stopped responding".** Check that Bluetooth is on. If it keeps happening on Linux, look at the kernel log:

```bash
journalctl -k | grep "tx timeout"
```

A line such as `command 0x2041 tx timeout`, followed by a USB disconnect, means the Bluetooth adapter crashed. Quak can't prevent
this. One USB Bluetooth 5.4 dongle did this more than 20 times in a day, on different ports, even with nothing else using it. The adapter
comes back switched off, so turn it on with `bluetoothctl power on`. If this happens a lot, try a different adapter.

**"Not seen within 45 s" after an adapter crash.** The same dongle sometimes came back claiming to be on, while hearing nothing at all,
with no error in the log. If your camera is on and nothing else nearby shows up in a Bluetooth scan either, turn Bluetooth off and on
again: `bluetoothctl power off`, then `bluetoothctl power on`.

## Wi-Fi, media and preview

**"Wi-Fi adapter X is this computer's network connection".** Quak won't take your connection without asking. Plug in a second
adapter, or allow it with `--wifi-adapter X`. The app asks you instead.

**"Joining the camera's Wi-Fi" takes a long time on a phone.** If the camera's Wi-Fi was off, the phone can take 30 seconds or more
to see it.

**The phone says it didn't join, "was the Connect prompt declined?"** Android asks once before joining the camera's network. If
you didn't see the prompt, try again: it can also mean the phone didn't find the network in time.

**The preview is blank.** The camera stops its stream while recording, which is why the panel says so. After you stop recording, or
after you change mode, the stream comes back within about 10 seconds and the picture within about 20.

**"The camera didn't start its preview".** The camera refused. Try again in a moment. Quak already clears a stream left running by
an app that was killed.

**"No picture came from the camera".** Quak started the preview, but no video arrived within 30 seconds. Try again. If it keeps
happening on a computer, check that a firewall allows incoming UDP on port 8554, which is where the camera sends its video.

**The camera's Wi-Fi stays on.** An app that's killed during preview can't turn it off. Run `quak ap off`.

**My computer lost its Wi-Fi after Quak was killed.** See [If Quak gets killed](safety.md#if-quak-gets-killed).

## The app

**`quak-gui` says there's no display.** You're on a machine with no screen, such as over SSH. `quak-gui --web` serves it to a browser
on the same machine.

**The first launch seems to hang.** The desktop client is about 45 MB and is downloaded once.

**The Android app won't update.** APKs signed with different keys can't update each other. Uninstall the old one first, which
removes its pairing and downloads.

## Known issues

**Livestream can stall when joining a network (experimental).** The camera connects to the Wi-Fi network and finishes the handshake,
but never asks for an IP address, so the join times out after 60 seconds. Livestream worked end to end on the test camera, including
twice on the day this started, then every later attempt stalled. On the affected camera:

| Tried | Result |
|---|---|
| Sleeping and waking the camera | Still stalls |
| Restarting the camera | Still stalls |
| Replugging the computer's Wi-Fi adapter | Still stalls |
| A network name the camera had never seen | Still stalls |
| A phone joining the same network | Works |

The network itself is fine, so the silence is on the camera's side. It isn't known why, or whether something Quak sent caused it.
**Reset Connections** on the camera hasn't been tried, because it also removes every pairing. If you hit this or get past it, please
[open an issue](https://github.com/ADCDS/quak/issues).

**A tap can occasionally be missed.** Some screens redraw completely when the camera reports a change, and a tap landing at that moment
is lost. Tap again. Media and Diagnostics no longer do this.

**Windows has never been run with a camera.** It's experimental, so please report what you find.
