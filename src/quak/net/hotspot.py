"""A throwaway Wi-Fi hotspot on this PC for livestream tests (Linux, NetworkManager).

The camera joins it instead of a home network, so no real credentials ever go to the
camera. The SSID and password are generated once and kept in ~/.config/quak/hotspot.json
(mode 0600), so the camera only ever remembers one extra network.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
from dataclasses import dataclass
from typing import AsyncIterator, Callable

from quak import config
from quak.core.errors import NetworkError
from quak.i18n import tr
from quak.net.nmcli import HOTSPOT_PROFILE, NetworkManager


@dataclass
class Hotspot:
    ssid: str
    password: str
    gateway: str
    device: str


def lab_credentials() -> tuple[str, str]:
    path = config.hotspot_file()
    try:
        data = json.loads(path.read_text())
        return data["ssid"], data["password"]
    except (OSError, ValueError, KeyError):
        pass
    ssid, password = f"quak-lab-{secrets.token_hex(2)}", secrets.token_urlsafe(15)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:  # private from the first byte
        json.dump({"ssid": ssid, "password": password}, f)
    tmp.replace(path)
    return ssid, password


@contextlib.asynccontextmanager
async def lab_hotspot(
    nm: NetworkManager | None = None, *, band: str | None = None, log: Callable[[str], None] = lambda s: None
) -> AsyncIterator[Hotspot]:
    nm = nm or NetworkManager()
    ssid, password = lab_credentials()
    if ssid in await asyncio.to_thread(nm.saved_wifi_ssids):
        raise NetworkError(tr("refusing to use {ssid}: a saved network has that name", ssid=repr(ssid)))
    device = await asyncio.to_thread(nm.pick_device, True)
    previous = device.connection if device.state.startswith("connected") and device.connection else None
    route_before = await asyncio.to_thread(nm.default_route_device)
    band = band or "bg"  # 2.4 GHz works whatever the adapter and the camera support
    log(f"starting hotspot {ssid} on {device.name} ({band})")
    # a saved profile for the camera's network would otherwise take the adapter from the hotspot
    autoconnect_was = await asyncio.to_thread(nm.pause_autoconnect, device.name, previous)
    try:
        await asyncio.to_thread(nm.hotspot_up, device.name, ssid, password, band)
        gateway = await asyncio.to_thread(nm.ipv4, device.name)
        if not gateway:
            raise NetworkError(tr("hotspot came up without an IPv4 address"))
        yield Hotspot(ssid, password, gateway, device.name)
    finally:
        await asyncio.to_thread(nm.down, HOTSPOT_PROFILE)
        if previous and previous != HOTSPOT_PROFILE:
            with contextlib.suppress(NetworkError):
                await asyncio.to_thread(nm.activate, previous, device.name)
        with contextlib.suppress(NetworkError):
            await asyncio.to_thread(nm.restore_autoconnect, device.name, autoconnect_was)
        route_after = await asyncio.to_thread(nm.default_route_device)
        if route_before and route_after != route_before:
            log(f"warning: default route moved from {route_before} to {route_after}")
