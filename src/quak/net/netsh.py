"""Windows Wi-Fi control via netsh, for joining the camera's access point.

Same interface as quak.net.nmcli.NetworkManager (pick_device, join, down, activate, ipv4,
default_route_device). Quak's profile is temporary (connectionMode manual), the XML file
with the password is deleted right after import, and the previous network is reconnected.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from xml.sax.saxutils import escape

from quak.core.errors import NetworkError
from quak.i18n import tr
from quak.net.profiles import CAMERA_PROFILE


def run_command(args: list[str], timeout: float = 60) -> str:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise NetworkError(f"{args[0]}: {e}") from None
    if p.returncode:
        raise NetworkError(
            tr("{command} failed: {output}", command=" ".join(args[:4]), output=(p.stdout + p.stderr).strip()[:300])
        )
    return p.stdout


@dataclass
class WifiDevice:
    name: str
    state: str
    connection: str
    ap_mode: bool = False
    band_5ghz: bool = True


def profile_xml(ssid: str, password: str, name: str = CAMERA_PROFILE) -> str:
    return f"""<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>{escape(name)}</name>
  <SSIDConfig><SSID><name>{escape(ssid)}</name></SSID></SSIDConfig>
  <connectionType>ESS</connectionType>
  <connectionMode>manual</connectionMode>
  <MSM><security>
    <authEncryption><authentication>WPA2PSK</authentication><encryption>AES</encryption><useOneX>false</useOneX></authEncryption>
    <sharedKey><keyType>passPhrase</keyType><protected>false</protected><keyMaterial>{escape(password)}</keyMaterial></sharedKey>
  </security></MSM>
</WLANProfile>"""


GUID = re.compile(r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.I)


def parse_interfaces(text: str) -> list[WifiDevice]:
    devices, cur = [], {}
    for line in text.splitlines():
        m = re.match(r"\s*([^:]+?)\s*:\s*(.*)$", line)
        if not m:
            continue
        key, value = m.group(1).strip().lower(), m.group(2).strip()
        if key == "name":
            if cur:
                devices.append(cur)
            cur = {"name": value}
        elif cur and key in ("state", "profile"):
            cur[key] = value
    if cur:
        devices.append(cur)
    if not devices:
        devices = _parse_interfaces_any_language(text)
    return [WifiDevice(d["name"], d.get("state", ""), d.get("profile", "")) for d in devices]


def _parse_interfaces_any_language(text: str) -> list[dict]:
    """netsh translates its labels and states. What stays the same: each adapter's name comes two lines before its
    GUID, and only a connected adapter has an SSID line."""
    devices: list[dict] = []
    values: list[str] = []
    for line in text.splitlines():
        m = re.match(r"\s*([^:]+?)\s*:\s*(.*)$", line)
        if not m:
            continue
        key, value = m.group(1).strip(), m.group(2).strip()
        if GUID.match(value) and len(values) >= 2:
            devices.append({"name": values[-2]})
        elif key == "SSID" and devices:
            devices[-1].update(state="connected", profile=value)
        values.append(value)
    return devices


class Netsh:
    takes_over_adapter = True  # the adapter leaves its current network while Quak uses it

    def __init__(self, runner=run_command) -> None:
        self.run = runner

    def wifi_devices(self) -> list[WifiDevice]:
        return parse_interfaces(self.run(["netsh", "wlan", "show", "interfaces"]))

    def pick_device(self, need_ap: bool = False) -> WifiDevice:
        if need_ap:
            raise NetworkError(tr("the lab hotspot isn't supported on Windows"))
        devices = self.wifi_devices()
        if not devices:
            raise NetworkError(tr("no Wi-Fi adapter found"))
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
        return next((d for d in devices if not d.state.startswith("connected")), devices[0])

    def join(self, device: str, ssid: str, password: str, name: str = CAMERA_PROFILE, timeout: int = 40) -> None:
        fd, path = tempfile.mkstemp(suffix=".xml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(profile_xml(ssid, password, name))
            self.run(["netsh", "wlan", "add", "profile", f"filename={path}", "user=current"])
        finally:
            os.unlink(path)
        self.run(["netsh", "wlan", "connect", f"name={name}", f"ssid={ssid}", f"interface={device}"])
        end = time.monotonic() + timeout  # that only starts the connection: wait for the address
        while not self.ipv4(device):
            if time.monotonic() > end:
                raise NetworkError(
                    tr(
                        "{device} didn't get an address from {ssid} within {seconds} s",
                        device=device,
                        ssid=ssid,
                        seconds=timeout,
                    )
                )
            time.sleep(1)

    def down(self, name: str) -> None:
        try:
            self.run(["netsh", "wlan", "delete", "profile", f"name={name}"])
        except NetworkError:
            pass

    def activate(self, name: str, device: str, timeout: int = 40) -> None:
        self.run(["netsh", "wlan", "connect", f"name={name}", f"interface={device}"])

    def ipv4(self, device: str) -> str | None:
        out = self.run(["netsh", "interface", "ipv4", "show", "addresses", f"name={device}"])
        m = re.search(r"\b(\d+\.\d+\.\d+\.\d+)\b", out)  # the labels are translated, and the address comes first
        return m.group(1) if m else None

    def default_route_device(self) -> str | None:
        return None  # not checked on Windows
