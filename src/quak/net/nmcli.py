"""NetworkManager control (Linux) for joining the camera's Wi-Fi and running a lab hotspot.

Rules: profiles Quak creates are named quak-*, never autoconnect, never take the default
route or DNS, and are deleted afterwards; the connection that was active on the adapter
before is brought back. Quak never edits or reads secrets of other profiles. While it uses
the adapter it pauses auto-connect on that device (a runtime flag, restored afterwards),
because a saved profile for the camera's network would otherwise take the adapter away.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from quak.core.errors import NetworkError
from quak.i18n import tr
from quak.net.profiles import CAMERA_PROFILE, HOTSPOT_PROFILE

Runner = Callable[[list[str]], str]


def run_command(args: list[str], timeout: float = 60) -> str:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise NetworkError(f"{args[0]}: {e}") from None
    if p.returncode:
        raise NetworkError(
            tr(
                "{command} failed: {output}",
                command=" ".join(_redact(args)),
                output=p.stderr.strip() or p.stdout.strip(),
            )
        )
    return p.stdout


def _pause_marker(device: str) -> Path:
    """Per boot, like the flag it records (NetworkManager forgets a device's autoconnect flag on reboot)."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return Path(runtime) / "quak" / f"autoconnect-paused-{device}"


def _read_marker(marker: Path) -> dict | None:
    try:
        state = json.loads(marker.read_text())
        return state if isinstance(state, dict) and "autoconnect" in state else None
    except (OSError, ValueError):
        return None


def _process_alive(pid: int | None) -> bool:
    if not pid or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # it exists but isn't ours
        return True
    return True


def _redact(args: list[str]) -> list[str]:
    out, hide = [], False
    for a in args:
        out.append("***" if hide else a)
        hide = a in ("wifi-sec.psk", "password")
    return out


def split_terse(line: str) -> list[str]:
    """Split one `nmcli -t` line on unescaped colons."""
    fields, cur, i = [], "", 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur += line[i + 1]
            i += 2
            continue
        if c == ":":
            fields.append(cur)
            cur = ""
        else:
            cur += c
        i += 1
    fields.append(cur)
    return fields


@dataclass
class WifiDevice:
    name: str
    state: str
    connection: str
    ap_mode: bool = False
    band_5ghz: bool = False


class NetworkManager:
    takes_over_adapter = True  # the adapter leaves its current network while Quak uses it (not so on a phone)

    def __init__(self, runner: Runner = run_command) -> None:
        self.run = runner

    def wifi_devices(self) -> list[WifiDevice]:
        out = []
        for line in self.run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"]).splitlines():
            f = split_terse(line)
            if len(f) >= 4 and f[1] == "wifi":
                dev = WifiDevice(f[0], f[2], f[3])
                props = self.run(["nmcli", "-t", "-f", "WIFI-PROPERTIES", "device", "show", f[0]])
                for p in props.splitlines():
                    k, _, v = p.partition(":")
                    if k == "WIFI-PROPERTIES.AP":
                        dev.ap_mode = v == "yes"
                    elif k == "WIFI-PROPERTIES.5GHZ":
                        dev.band_5ghz = v == "yes"
                out.append(dev)
        return out

    def pick_device(self, need_ap: bool = False) -> WifiDevice:
        """The adapter named by QUAK_WIFI_ADAPTER, else one this computer isn't using for its network."""
        devices = [d for d in self.wifi_devices() if d.ap_mode or not need_ap]
        if not devices:
            raise NetworkError(
                tr("no Wi-Fi adapter with access-point mode found") if need_ap else tr("no Wi-Fi adapter found")
            )
        wanted = os.environ.get("QUAK_WIFI_ADAPTER")
        if wanted:
            for d in devices:
                if d.name == wanted:
                    return d
            raise NetworkError(
                tr(
                    "Wi-Fi adapter {adapter} not found (found: {found})",
                    adapter=wanted,
                    found=", ".join(d.name for d in devices),
                )
            )
        main = self.default_route_device()
        return next((d for d in devices if d.name != main), devices[0])

    def saved_wifi_ssids(self) -> set[str]:
        names = [
            split_terse(l) for l in self.run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"]).splitlines()
        ]
        ssids = set()
        for f in names:
            if len(f) >= 2 and f[1] == "802-11-wireless" and not f[0].startswith("quak-"):
                ssid = self.run(["nmcli", "-g", "802-11-wireless.ssid", "connection", "show", f[0]]).strip()
                if ssid:
                    ssids.add(ssid)
        return ssids

    def pause_autoconnect(self, device: str, previous: str | None = None) -> bool:
        """Stop saved profiles taking the adapter while Quak uses it, and note how to undo that and what was active,
        so that a later run can clean up if this one gets killed (see recover)."""
        marker = _pause_marker(device)
        state = _read_marker(marker)
        if state is not None:  # left by a Quak that was killed: that one knows the real state
            was_on = state["autoconnect"]
        else:
            was_on = self.run(["nmcli", "-g", "GENERAL.AUTOCONNECT", "device", "show", device]).strip() != "no"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"autoconnect": was_on, "previous": previous, "pid": os.getpid()}))
        self.run(["nmcli", "device", "set", device, "autoconnect", "no"])
        return was_on

    def restore_autoconnect(self, device: str, was_on: bool) -> None:
        self.run(["nmcli", "device", "set", device, "autoconnect", "yes" if was_on else "no"])
        _pause_marker(device).unlink(missing_ok=True)

    def recover(self) -> list[str]:
        """Undo what a killed Quak left behind on its adapters: its network, the paused auto-connect, and the
        connection it replaced. Adapters a running Quak is using are left alone. Returns the ones recovered."""
        recovered = []
        for marker in _pause_marker("x").parent.glob("autoconnect-paused-*"):
            state = _read_marker(marker)
            if state is None or _process_alive(state.get("pid")):
                continue
            device = marker.name.removeprefix("autoconnect-paused-")
            self.down(CAMERA_PROFILE)
            self.down(HOTSPOT_PROFILE)
            self.restore_autoconnect(device, state["autoconnect"])
            if state.get("previous"):
                with contextlib.suppress(NetworkError):
                    self.activate(state["previous"], device)
            recovered.append(device)
        return recovered

    def delete_profile(self, name: str) -> None:
        try:
            self.run(["nmcli", "connection", "delete", name])
        except NetworkError:
            pass

    def join(self, device: str, ssid: str, password: str, name: str = CAMERA_PROFILE, timeout: int = 40) -> None:
        self.delete_profile(name)
        self.run(
            [
                "nmcli",
                "connection",
                "add",
                "type",
                "wifi",
                "ifname",
                device,
                "con-name",
                name,
                "ssid",
                ssid,
                "autoconnect",
                "no",
                "wifi-sec.key-mgmt",
                "wpa-psk",
                "wifi-sec.psk-flags",
                "2",
                "ipv4.method",
                "auto",
                "ipv4.never-default",
                "yes",
                "ipv4.ignore-auto-dns",
                "yes",
                "ipv6.method",
                "disabled",
            ]
        )
        self._up_with_password(name, device, password, timeout)

    def hotspot_up(
        self, device: str, ssid: str, password: str, band: str = "bg", name: str = HOTSPOT_PROFILE, timeout: int = 40
    ) -> None:
        self.delete_profile(name)
        self.run(
            [
                "nmcli",
                "connection",
                "add",
                "type",
                "wifi",
                "ifname",
                device,
                "con-name",
                name,
                "ssid",
                ssid,
                "autoconnect",
                "no",
                "802-11-wireless.mode",
                "ap",
                "802-11-wireless.band",
                band,
                "wifi-sec.key-mgmt",
                "wpa-psk",
                "wifi-sec.psk-flags",
                "2",
                "wifi-sec.proto",
                "rsn",
                "wifi-sec.pairwise",
                "ccmp",
                "wifi-sec.group",
                "ccmp",
                "wifi-sec.pmf",
                "disable",
                "ipv4.method",
                "shared",
                "ipv6.method",
                "disabled",
            ]
        )
        self._up_with_password(name, device, password, timeout)

    def _up_with_password(self, name: str, device: str, password: str, timeout: int) -> None:
        """A password on a command line is visible to every user through `ps`, so it goes in a private file."""
        fd, path = tempfile.mkstemp(prefix="quak-")  # only this user can read it
        try:
            with os.fdopen(fd, "w") as f:
                f.write("802-11-wireless-security.psk:" + password.replace("\\", "\\\\") + "\n")
            self.run(["nmcli", "--wait", str(timeout), "connection", "up", name, "ifname", device, "passwd-file", path])
        finally:
            Path(path).unlink(missing_ok=True)

    def down(self, name: str) -> None:
        try:
            self.run(["nmcli", "connection", "down", name])
        except NetworkError:
            pass
        self.delete_profile(name)

    def activate(self, name: str, device: str, timeout: int = 40) -> None:
        self.run(["nmcli", "--wait", str(timeout), "connection", "up", name, "ifname", device])

    def ipv4(self, device: str) -> str | None:
        for line in self.run(["nmcli", "-t", "-f", "IP4.ADDRESS", "device", "show", device]).splitlines():
            _, _, v = line.partition(":")
            if v:
                return v.split("/")[0]
        return None

    def default_route_device(self) -> str | None:
        try:
            parts = self.run(["ip", "route", "get", "1.1.1.1"]).split()
        except NetworkError:  # no route at all: a laptop in the field, the normal case for a camera
            return None
        return parts[parts.index("dev") + 1] if "dev" in parts else None
