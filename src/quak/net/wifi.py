"""Get this machine onto the camera's Wi-Fi (or reuse a working connection) and clean up after."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import sys
from typing import AsyncIterator, Callable

from quak.camera import Camera
from quak.core.errors import AdapterInUse, NetworkError
from quak.core.registry import Status
from quak.i18n import tr
from quak.net.http import CAMERA_AP_URL, CameraHttp
from quak.net.profiles import CAMERA_PROFILE

_platform_joiner = None


def set_platform_joiner(joiner) -> None:
    """The app registers the phone's joiner here (it needs the page's QuakWifi service)."""
    global _platform_joiner
    _platform_joiner = joiner


def default_joiner():
    if _platform_joiner is not None:
        return _platform_joiner
    if sys.platform.startswith("linux"):
        from quak.net.nmcli import NetworkManager

        return NetworkManager()
    if sys.platform == "win32":
        from quak.net.netsh import Netsh

        return Netsh()
    return None


def recover_network() -> list[str]:
    """Undo what a killed Quak left on this computer's network. Safe to call at every start."""
    if not sys.platform.startswith("linux"):
        return []
    from quak.net.nmcli import NetworkManager

    with contextlib.suppress(NetworkError):
        return NetworkManager().recover()
    return []


async def _call(fn, *args):
    """Joiner methods: the phone's are coroutines; nmcli/netsh block, so they run in a thread."""
    if inspect.iscoroutinefunction(fn):
        return await fn(*args)
    return await asyncio.to_thread(fn, *args)


@contextlib.asynccontextmanager
async def camera_wifi(
    cam: Camera,
    *,
    base_url: str | None = None,
    mode: str = "auto",
    joiner=None,
    allow_busy_adapter: bool = False,
    log: Callable[[str], None] = lambda s: None,
) -> AsyncIterator[CameraHttp]:
    """mode: 'auto' = use the camera's HTTP API if reachable, else join its AP;
    'assume' = just use base_url (simulator, USB, or a connection set up by hand).
    Raises AdapterInUse rather than taking the adapter this computer is online through, unless allowed."""
    api = CameraHttp(base_url or CAMERA_AP_URL)
    if mode == "assume" or await asyncio.to_thread(api.reachable):
        log(f"camera HTTP API reachable at {api.host}:{api.port}")
        yield api
        return
    joiner = joiner or default_joiner()
    if joiner is None:
        raise NetworkError(
            tr(
                "the camera's Wi-Fi isn't reachable and this platform can't join it automatically; "
                "connect to the camera's Wi-Fi by hand and use --wifi assume"
            )
        )
    device = await _call(joiner.pick_device)
    previous = device.connection if device.state.startswith("connected") and device.connection else None
    route_before = await _call(joiner.default_route_device)
    named = os.environ.get("QUAK_WIFI_ADAPTER") == device.name
    in_use = device.name == route_before or (route_before is None and previous is not None)
    if getattr(joiner, "takes_over_adapter", False) and in_use and not (allow_busy_adapter or named):
        raise AdapterInUse(device.name)
    ap_was_on = bool(await cam.status(Status.AP_MODE))
    # before the camera's Wi-Fi comes up: a saved profile for it would otherwise grab the adapter
    pause = getattr(joiner, "pause_autoconnect", None)
    autoconnect_was = await _call(pause, device.name, previous) if pause else None
    try:
        if not ap_was_on:
            log("turning the camera's Wi-Fi on")
            await cam.set_ap(True)
            await cam.wait_status(Status.AP_MODE, lambda v: v == 1, 20)
        ssid, password = await cam.wifi_credentials()
        last = None
        attempts = getattr(joiner, "join_attempts", 3)
        for attempt in range(attempts):  # the AP can take a few seconds to show up after switching on
            try:
                log(f"joining the camera's Wi-Fi on {device.name} (attempt {attempt + 1})")
                await _call(joiner.join, device.name, ssid, password)
                break
            except NetworkError as e:
                last = e
                if attempt + 1 < attempts:
                    await asyncio.sleep(3)
        else:
            raise last
        api.source_ip = await _call(joiner.ipv4, device.name)
        for _ in range(20):
            if await asyncio.to_thread(api.reachable):
                break
            await asyncio.sleep(0.5)
        else:
            raise NetworkError(tr("joined the camera's Wi-Fi but its HTTP API doesn't answer"))
        yield api
    finally:
        await _call(joiner.down, CAMERA_PROFILE)
        if previous and previous != CAMERA_PROFILE:
            with contextlib.suppress(NetworkError):
                await _call(joiner.activate, previous, device.name)
        if not ap_was_on:
            with contextlib.suppress(Exception):
                await cam.set_ap(False)
        if autoconnect_was is not None:
            with contextlib.suppress(NetworkError):
                await _call(joiner.restore_autoconnect, device.name, autoconnect_was)
        route_after = await _call(joiner.default_route_device)
        if route_before and route_after != route_before:
            log(f"warning: default route moved from {route_before} to {route_after}")
