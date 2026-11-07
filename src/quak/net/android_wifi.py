"""Phone Wi-Fi joiner: the same interface as nmcli/netsh, on top of the QuakWifi Flet service.

Android joins the camera's access point for this app only (WifiNetworkSpecifier) and binds the
app's traffic to it, so there is no profile to delete, no previous connection to restore and no
source address to bind: releasing the request puts the phone back on its own network.
"""

from __future__ import annotations

import contextlib

from quak.core.errors import NetworkError
from quak.net.netsh import WifiDevice
from quak.net.profiles import CAMERA_PROFILE


class AndroidJoiner:
    join_attempts = 1  # the phone's request already waits for the AP; retrying would re-prompt the user

    def __init__(self, wifi) -> None:
        self.wifi = wifi  # quak_native.QuakWifi (or a stand-in with the same methods)

    async def pick_device(self, need_ap: bool = False) -> WifiDevice:
        if need_ap:
            raise NetworkError("the lab hotspot isn't available on a phone")
        return WifiDevice("wifi", "app-scoped", "")

    async def join(self, device: str, ssid: str, password: str, name: str = CAMERA_PROFILE, timeout: int = 120) -> None:
        r = await self.wifi.join(ssid, password, timeout)
        if not r.get("joined"):
            raise NetworkError("the phone didn't join the camera's Wi-Fi (was the Connect prompt declined?)")
        if not r.get("bound"):
            await self.down(name)
            raise NetworkError("joined the camera's Wi-Fi but couldn't send the app's traffic over it")

    async def down(self, name: str) -> None:
        with contextlib.suppress(Exception):
            await self.wifi.release()

    async def activate(self, name: str, device: str, timeout: int = 40) -> None:
        pass  # Android returns to the previous network on its own

    async def ipv4(self, device: str) -> str | None:
        return None  # the app is bound to the camera's network; no source address needed

    async def default_route_device(self) -> str | None:
        return None
