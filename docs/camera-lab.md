# Camera Lab

The Camera Lab is an advanced, Linux-only set of commands for a HERO11 Black that **already has an SSH
login of its own**. It lets you log in over SSH, switch the USB-C port to host mode, see what is on the
USB bus, and read or copy from storage the camera has mounted.

> **This is for a camera you have already opened up yourself.** Quak does not root a camera, and does not
> install, stage or serve any software on it. The one and only thing Quak sends a camera that is not over
> an authenticated SSH login is a single request that *arms* an SSH service already present on the card
> (see [Arm](#arm-quak-ssh-arm)). If that service isn't there, the camera simply never answers on port 22
> and Quak tells you so. Everything else in this page happens over a normal, key-authenticated SSH session
> that you control.

It is early, built around one rooted HERO11 Black, and every hardware-changing action is explicit and, where
the hardware allows, reversible. Read [Limitations](#limitations) and the [Honesty matrix](#honesty-matrix)
before you rely on any of it.

## Prerequisites

- **A camera that already exposes SSH.** The arm step triggers a service that is already on the camera's SD
  card. Quak neither provides nor installs it.
- **Linux.** The Lab's USB-NCM discovery and host-mode switching are written for Linux. The command line
  uses the system OpenSSH client (`ssh`, `ssh-keygen`, `ssh-keyscan`); install the OpenSSH client package
  for your distribution.
- **An SSH key.** Generate one with `quak ssh key generate` (Ed25519), or import an existing key with
  `quak ssh key import`. Quak logs in with keys by default; private keys live in `~/.config/quak/ssh/keys/`
  with `0600` permissions, and key material never appears in logs or in Quak's JSON.
- **A way to reach the camera.** The camera's own Wi-Fi, its USB-NCM gadget, or any host/IP you give
  (USB-NCM, Ethernet on a hub, or a LAN). See [How Quak finds the camera](#how-quak-finds-the-camera).

Everything below is also available in the desktop and Android app, which uses the Dart `dartssh2` SSH client
and an `xterm` terminal view instead of the system `ssh`; the two transports share the same profiles, keys
and host-key pins.

## SSH

`quak ssh ...` is authenticated access to the camera. Common options:

- `--address` — the camera's BLE address (defaults to the paired camera, or `$QUAK_CAMERA_ADDRESS`).
- `--host HOST` — reach the camera at this host/IP for this run only (overrides the saved profile).
- `--transport auto|wifi|usb|host` — the route for this run only.
- `--json` — machine-readable output.
- `--yes` — confirm an action that would change something.

Errors map to exit codes: `8` SSH, `9` host key, `10` SSH key, `11` arm failed.

### Config (`quak ssh profile`)

Quak keeps one SSH profile per camera, keyed by the camera's BLE address, in `~/.config/quak/ssh.json`
(written `0600`). A profile records the transport, the auth mode (`key`, `password` or `none`), the SSH and
HTTP ports, the login user (`root` by default), and which stored key to use.

```bash
quak ssh profile show              # the saved profile for this camera
quak ssh profile set --auth password --user root
quak ssh profile set --auth key    # back to the stored key
quak ssh profile forget --yes      # delete it
```

`--host` and `--transport` on any `quak ssh`/`quak lab` command override the saved profile for that one run
and are not written back.

### Password login

Password login works **alongside** keys; it does not replace the key store. Set the profile to
`--auth password`, then pass the password on standard input:

```bash
quak ssh profile set --auth password --user root
quak ssh test --password-stdin          # one line on stdin; never an argument
quak ssh exec --password-stdin -- id    # one line on stdin, then the command
quak lab status --password-stdin        # one line on stdin, for this one action
quak ssh test --blank-password          # dropbear -B / AUTH=blank, an empty password
```

The password is not stored in the profile or anywhere in Quak. For the system `ssh` transport Quak writes a
temporary `SSH_ASKPASS` helper (mode `0700`), puts the password in `QUAK_SSH_PASSWORD` for that child only,
runs `ssh` without a terminal, and removes the helper afterwards. The password is never in `argv` or in the
JSON output. `ssh` must be OpenSSH 8.4 or newer; the helper is a `/bin/sh` script, so this path is Unix-only.

The app's key rows have **Install on camera** and **Forget**: install adds that key to the camera and proves
it logs in (using an already accepted key), forget removes the key from Quak's store and clears it from any
profile that pointed at it. On a phone with no system `ssh`, **Test** uses the Dart client instead and can log
in with a key or password; the terminal does the same.

In the app, the SSH tab has a **Key / Password** choice. Password mode asks for the password when Test or the
embedded terminal needs it and keeps it only for that login. **Leave the field empty for gopro-root's default
`AUTH=blank` camera** (`dropbear -B`). The phone's embedded terminal uses `dartssh2` directly, so it does not
need the system `ssh` binary. A Lab action on a password profile asks once, and keeps the password only for
that action.

Encrypted keys are a known limit in this build: an `ssh-keygen` key with a passphrase imported into the app
reported `All authentication methods failed`, while the same key authenticated from OpenSSH. Use an
unencrypted key, the blank password, or a password profile until the Dart encrypted-OpenSSH-key path is
fixed.

`--host` and `--transport` apply per run; `profile set` changes only the saved `auth` and `user`.

### Keys (`quak ssh key`)

The key store is local; only `install`, `rotate` and `revoke` talk to the camera.

```bash
quak ssh key list
quak ssh key generate --label laptop            # Ed25519; --passphrase-stdin to set one
quak ssh key import ~/.ssh/id_ed25519 --label laptop
quak ssh key show ID                            # public key and fingerprint
quak ssh key install [ID]                       # add a key to the camera and prove it logs in
quak ssh key rotate OLD NEW --yes               # add NEW, prove it, then remove OLD
quak ssh key revoke ID --yes                    # remove a key from the camera
quak ssh key delete ID --yes                    # remove a key from Quak's store only
```

Camera-side key changes are careful: Quak reads both the card's authoritative
`/tmp/fuse_d/authorized_keys` and the running server's `/dev/dropbear/authorized_keys`, keeps every existing
line, writes to a temporary file, verifies it, backs up what was there, renames it into place and syncs, then
copies it to the runtime file and reads it back. It only ever **reads** `dropbear.conf` (to notice a
`keyonly` login mode) and never edits it. It refuses to remove the last remaining key when the camera only
accepts keys, so a mistake can't lock you out. A rotation proves the new key with a fresh login before it
removes the old one, and restores its backup if anything fails.

### Host keys (`quak ssh host`)

Quak pins the camera's SSH host key and refuses to connect to a key it hasn't pinned, or to one that has
changed since. The command line also keeps an OpenSSH `known_hosts` file (in `~/.config/quak/ssh/`) so the
system `ssh` enforces the same decision; the app pins the `SHA256:` fingerprint that `dartssh2` exposes.

```bash
quak ssh host show                 # the pinned key and its state: unknown / trusted / changed
quak ssh host trust                # pin the key the camera offers now (check the fingerprint first)
quak ssh host forget --yes         # drop the pin and the known_hosts entry
```

A **changed** host key is never replaced automatically: forget it, confirm you expected the change, then
trust the new one.

### Arm (`quak ssh arm`)

This is the only camera action that is not over SSH. It sends one unauthenticated request —
`GET /gp/gpCamera?cmd=SC&p=%02` — which arms the SSH service already on the card. Quak sends no other HTTP
request, uploads nothing, and serves nothing.

```bash
quak ssh arm
```

An `HTTP 200` is accepted; an `HTTP 500` (the camera's network stack busy) is retried a few times, ten
seconds apart. Quak then waits for something to answer with an SSH banner on port 22 and reports
`http_status`, `accepted`, `ssh_up`, `banner` and a message. If the request is accepted but nothing comes up
on port 22, that usually means the service on the card is missing or stale — which Quak does not fix.

### Status, test, exec, open

```bash
quak ssh status                    # profile, endpoint, SSH banner, host-key state — never joins Wi-Fi
quak ssh test                      # log in and show id and uname
quak ssh exec -- uname -a          # run one remote command; its exit code is Quak's exit code
quak ssh open                      # an interactive SSH terminal in your current terminal
```

### How Quak finds the camera

`--transport` (or the profile's transport) chooses the route:

- `host` — the stored host/IP, as typed.
- `usb` — the camera's own CDC-NCM interface (USB vendor `2672`) in `/sys/class/net`, with the camera's
  address derived from its serial or from the interface's `/24`. This is the camera's gadget, not a hub.
- `wifi` — the camera's access point at `10.5.5.9`. Quak joins it (over a short-lived BLE session, like the
  rest of Quak's Wi-Fi use) only for the duration of the operation.
- `auto` — a reachable explicit host first, then USB, then Wi-Fi.

## Lab

`quak lab ...` builds on the same SSH route. It has no simulated camera: `--sim` is refused, so a Lab command
can never change a camera you didn't mean to touch.

### Host mode (`quak lab host`)

The camera's USB-C port is normally a *device* port (it is the gadget your computer sees). Host mode turns it
into a *host* port, so the camera can drive USB devices you plug into it. Quak switches it with GoPro's own
in-kernel `dwc3_milbeaut` switch. **The switch lives entirely in the camera's RAM: a reboot puts the port
back by itself.** Nothing here formats, flashes or partitions anything.

```bash
quak lab host on --yes             # switch to host mode
quak lab host off --yes            # unmount Lab volumes, unload the toolkit, back to device mode
quak lab host recover              # finish a switch that was interrupted
```

Before switching on, Quak writes a marker to the card, then pauses the camera's USB daemons, detaches the
gadget, flips the switch and waits for the xHCI root hubs. If any step fails it puts the camera back the way
it was. Switching **off** undoes only what is actually there, so it is safe to run on a camera that is already
in device mode. If a switch is interrupted — most often because the SSH link ran over the very port being
switched — the marker lets `quak lab host recover` (or a reboot) finish the job.

`host on` **refuses** when your SSH connection runs over the camera's own USB-NCM, because the switch would
drop that connection with nothing left to reach the camera through. Connect over Wi-Fi or Ethernet first.

### Devices (`quak lab devices`)

Lists what is on the camera's USB bus — read from sysfs, changing nothing — and classifies each device
(audio, storage, network, HID, video, hub, serial, vendor-specific or other) from its interfaces and bound
drivers. The bus is only populated in host mode.

### Toolkit (`quak lab toolkit`)

Quak bundles a small toolkit of kernel modules (USB audio, USB storage and the r8152 USB-Ethernet driver) and
the tinyalsa tools, built for this camera's kernel (Linux 4.19.124). It ships inside Quak; see
[THIRD_PARTY_NOTICES.md](https://github.com/ADCDS/quak/blob/main/THIRD_PARTY_NOTICES.md) for its licences
(the modules are GPL-2.0-only; the source and build recipe are in `src/quak/lab/toolkit/core/SOURCE.txt`).

```bash
quak lab toolkit list              # what the card says is installed
quak lab toolkit install --yes     # copy the pack to the SD card and verify every file (loads nothing)
quak lab toolkit load              # insmod the modules, in dependency order, into the running kernel
quak lab toolkit unload            # rmmod them, in reverse
quak lab toolkit remove --yes      # unload, then delete the pack from the card
quak lab toolkit install-pack PATH --signers PATH --yes   # a locally supplied, signed pack
quak lab toolkit install-pack-url URL --sha256 SHA --signers PATH --yes   # a signed pack from https/file
```

A signed optional pack must carry `manifest.json` and a detached `manifest.sig`. Quak verifies the
signature with `ssh-keygen -Y verify` against your allowed-signers file before it reads a hash or touches the
camera, then checks the manifest, every module/tool hash, the camera's kernel/vermagic, uploads, rechecks the
hashes on the card, and writes the state last. It never loads a module. Local packs can use any id; installing
one replaces Quak's record of the core pack, so reinstall core before loading it. Every module, tool and
firmware file must carry a SHA-256 in the signed manifest; firmware without one is refused.

The URL form accepts only `https://` and local `file://` sources (no plain HTTP), checks the SHA-256 you give
before unpacking, and refuses zip entries that are absolute, traverse `..`, are symlinks or special files, or
fall outside `manifest.json`, `manifest.sig`, `modules/`, `tools/` and `firmware/`. The download, unpack and
install each have size caps; temporary files are removed afterwards.

In the app, the Lab **Overview** has a Toolkit card with Install, Load, Unload and Remove, each confirmed
first; Load shows the pack's kernel and vermagic and warns that a mismatched module can crash the camera.

Installing only writes to the card; it never loads a module. Loading is always explicit, verifies every
file's SHA-256 on the camera first, and **refuses a pack whose kernel or vermagic doesn't match the running
kernel**, because a module built for another kernel can crash the camera. If a module won't load, Quak takes
back out every module it had loaded.

### Storage (`quak lab storage`)

Find volumes the camera can see, mount them **read-only** under `/tmp/quak/mnt/`, list a directory, and copy
files between the camera's SD card and a mounted volume.

```bash
quak lab storage list              # volumes, with label, filesystem and size
quak lab storage list /tmp/fuse_d  # a directory listing
quak lab storage mount DEVICE --yes
quak lab storage copy SRC DST --yes
quak lab storage umount MOUNTPOINT --yes
quak lab storage eject MOUNTPOINT --yes
```

Mounts are read-only by default, and always `nosuid,nodev,noexec`. A copy never replaces an existing file; a
single file takes its real name only after its SHA-256 matches the source on both ends. **Quak has no way to
format, repartition, or delete the contents of a volume**, and copies only ever go between the SD card and a
mounted Lab volume.

### System (`quak lab system`)

Read-only status, kernel log and process list.

```bash
quak lab system            # uptime, load, memory, temperatures and SD-card space
quak lab system logs       # the last lines of dmesg
quak lab system processes  # ps
```

### Audio (`quak lab audio`)

Lists the camera's ALSA capture cards, records from a USB microphone into the camera's RAM with the bundled
`tinycap`, and copies the WAV to the card with a SHA-256 check.

```bash
quak lab audio list
quak lab audio record 0 --seconds 30 --yes
quak lab audio merge CLIP WAV OUT --offset 0.25
```

A recording is kept under 300 s and a header-only WAV is treated as a failure. `merge` runs on the computer
(not the camera) with `ffmpeg`, mixes the microphone track into the clip, and never overwrites an output.
In the app, **Record mic** and **Record with camera** do the same; the second starts the camera, records the
microphone for the chosen duration, and stops the camera in a `finally`.

### Network (`quak lab network`)

Lists the camera's interfaces (Ethernet, Wi-Fi, USB and other) and configures one for DHCP or a static
address. This is useful for a USB-C Ethernet adapter or a USB Wi-Fi stick behind the hub.

```bash
quak lab network list
quak lab network configure eth0 --dhcp --yes
quak lab network configure eth0 --ip 192.168.1.50 --prefix 24 --gateway 192.168.1.1 --yes
```

Changing the interface that carries the current SSH link can drop it. Quak warns, does not retry, and the
change may still have applied; reconnect and run `quak lab network list`.

### Display and serial

The Display tab keeps the honest support matrix (Media Mod HDMI works; USB DisplayLink is experimental;
USB-C HDMI/DP does not work in host mode) and lists Linux framebuffers if the camera exposes one. A
framebuffer can only be written when a matching driver exists; Quak does not flash or change the display
mode.

The Serial tab lists `/dev/ttyUSB*`, `/dev/ttyACM*` and `/dev/ttyS*` with their driver and USB IDs, and shows
the exact `microcom` command to run in the SSH terminal. Quak does not open the port itself, because opening
a serial device can reset it. The bundled kernel pack does not include USB-serial drivers (`ftdi_sio`,
`cp210x`, `cdc_acm`); they work only if the camera's own kernel has them.

## Limitations

- **The camera must already expose SSH.** Quak never provides or installs that; without it, `quak ssh arm`
  reports that nothing came up on port 22.
- **Host mode is RAM-only and can drop its own link.** A reboot reverts it. If the control link ran over the
  USB-C port, switching can cut it off mid-way; the camera finishes on its own, and `quak lab host recover`
  reports where it ended up.
- **Toolkit modules are built for one exact kernel** (4.19.124, with its vermagic). Quak refuses to load a
  pack that doesn't match, rather than risk a crash.
- **Audio capture goes to the camera's RAM** (`/tmp`, ~82 MB), never straight to the SD card, because
  `tinycap` aimed at the card leaves a header-only file. A power cycle loses what is in `/tmp`.
- **Linux only**, and the command line needs the OpenSSH client tools.

## Honesty matrix

What each capability actually does to the camera, and how you undo it. Quak reports exactly this and no more;
a value the camera can't give is shown as unknown rather than guessed.

| Capability | Changes the camera? | How it's kept safe / undone |
|---|---|---|
| `ssh status` | No | Reads only; never joins Wi-Fi. |
| `ssh arm` | Arms an SSH service already on the card | One documented unauthenticated request; no upload, no server. |
| `ssh test` / `exec` / `open` | No (runs your command) | A plain SSH session; strict host-key checking. |
| `ssh host trust` / `forget` | No (local pin) | Edits Quak's own store and `known_hosts`. |
| `ssh key install` / `rotate` / `revoke` | Edits `authorized_keys` on the card and runtime | Temp-write, verify, back up, prove the new key before removing the old, restore on failure, refuse last-key lockout. |
| `lab host on` / `off` | Flips the USB-C port mode (in RAM) | Reversible; a reboot reverts; marker + `recover` for an interrupted switch; refuses over USB-NCM. |
| `lab devices` | No | Reads sysfs only. |
| `lab toolkit install` | Writes files to the SD card | Verified by SHA-256; `remove` deletes them. |
| `lab toolkit load` / `unload` | Inserts/removes kernel modules (in RAM) | Kernel/vermagic gated; reverse-order rollback; a reboot unloads. |
| `lab storage mount` | Mounts a volume, read-only | `nosuid,nodev,noexec`; `umount`/`eject` undoes it. |
| `lab storage copy` | Writes one file to the card or a mounted volume | Never overwrites; SHA-256 verified end to end. |
| `lab system` | No | Reads status, dmesg and `ps` only. |

**What's verified.** The whole backend is covered by unit tests that use fake SSH and a fake camera; there is
no real `ssh` and no hardware in the test suite. The flows above were designed against one rooted HERO11
Black, but have not been through the same hardware test cycle as Quak's core camera features (see
[what has been tested](https://github.com/ADCDS/quak/blob/main/README.md#what-has-been-tested)). Treat the
Camera Lab as experimental, and keep a way back in (a second trusted key, and physical access to reboot)
before you change anything.
