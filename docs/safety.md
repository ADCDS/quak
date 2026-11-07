# What Quak changes, stores and can delete

Quak controls a real camera and can touch your computer's Wi-Fi. This page says exactly what it does.

## On your computer

**Bluetooth:** Quak pairs with the camera the normal way, through your system's Bluetooth. It doesn't change anything else.

**Wi-Fi:** preview, media and downloads need the camera's own Wi-Fi network.

- **Phone:** Android connects just for the app, and your other apps stay on your normal network.
- **Computer:** a Wi-Fi adapter joins the camera's network for a while, and leaves any network it was on until Quak is done.

On a computer, Quak:

- Prefers an adapter you aren't online through. If the only adapter is the one you're using, the app asks first, and the command
  line stops with an error until you choose it with `--wifi-adapter NAME`.
- On Linux, creates a temporary NetworkManager profile named `quak-camap`, which never takes your default route or DNS, and
  deletes it afterwards. It doesn't edit your other profiles.
- On Linux, turns off auto-connect on that adapter while using it, because a saved profile for the camera could otherwise take
  the adapter back. Auto-connect is restored afterwards.
- Reconnects your previous network when it's done. The app does this about a minute after you leave Media or stop the preview.

### If Quak gets killed

The command line cleans up on Ctrl-C and on `kill`, and it puts back what it changed. If it's killed harder, or the machine loses
power mid-use, Quak remembers what it changed and undoes it the next time it starts on Linux. On Windows, there's no automatic
recovery.

To recover by hand on Linux, where `DEVICE` is your Wi-Fi adapter:

```bash
nmcli connection delete quak-camap
nmcli device set DEVICE autoconnect yes
nmcli device connect DEVICE
```

A restart also clears the auto-connect change, and leaves only the `quak-camap` profile to delete.

### What's stored

| What | Where | Holds |
|---|---|---|
| Paired cameras, settings | `~/.config/quak` on Linux, `%APPDATA%\quak` on Windows, the app's private storage on Android | Camera addresses, models, app preferences |
| Downloads | `~/Videos/Quak` for the desktop app, the app's private storage on Android, and `--out` or the current folder for the command line | Your clips |
| Test data | `~/.local/state/quak` on Linux, `%LOCALAPPDATA%\quak` on Windows | Only when you run the test suites |

Quak doesn't store your Wi-Fi passwords or stream keys. It sends them to the camera when you ask it to join a network or stream.

### What's in logs

The Diagnostics screen, its exported log and `quak -v` show the bytes exchanged with the camera. They leave out passwords, stream
keys, network names, the camera's serial number and your computer's name, and they say how many bytes were hidden.

The camera's Bluetooth address can still appear in error messages and logs, so review a log before you share it publicly.

### The app's network access

`quak-gui --web` serves the app in a browser, on this computer only. **There's no login**, and anyone who can reach it can record
on and delete from your camera, so `--host` only makes sense on a network you fully trust.

## On the camera

**Quak tells the camera your computer's name** when pairing, as the name the camera shows for this device.

**A scheduled capture changes settings and leaves them changed.** Arming one:

- Switches the camera to Pro controls, because the camera hides scheduled capture in Easy
- Loads the Video preset you choose
- Sets Video Duration, which **also limits recordings you start by hand**
- Syncs the camera's clock

Quak tells you when arming changes these. Change Duration back in the camera's settings, or in Quak, when you're done.

**Livestream:**

- The camera joins the Wi-Fi network you choose and stores it. Only **Reset Connections** on the camera removes a stored network,
  and that also removes all its pairings.
- Your network's password and your stream URL, which holds your stream key, are sent to the camera over Bluetooth.
- With a plain `rtmp://` URL, the stream key and video travel unencrypted to the server.

**Camera Wi-Fi** is turned on when needed, and back off when Quak is done, if Quak was the one to turn it on. An app that's killed
during preview can leave it on: run `quak ap off`, or turn it off on the camera.

## What Quak can delete

Only clips you choose. Deleting one always asks for confirmation in the app, and needs `--yes` on the command line. Deleted clips
can't be recovered.

Quak has no way to format a card, delete everything, or factory-reset a camera.

Downloads never replace an existing file: a second `GX010001.MP4` is saved as `GX010001_1.MP4`. A download that gets cut off is
reported as an error, and isn't saved as if it were complete.

## For developers

`QUAK_DEV=1` shows the app's self-test and Live's test mode. They change much more, so read [testing.md](testing.md) first:

- **Self-test:** takes about 30 minutes, records clips, changes settings and restores them, and uses the Wi-Fi adapter.
- **Test mode:** turns this computer into a hotspot, and the camera stores that network.
