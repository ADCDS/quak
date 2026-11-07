"""Python side of the QuakWifi service (Dart side: src/flutter/quak_native/lib/src/quak_wifi_service.dart)."""

from typing import Any

import flet as ft


@ft.control("QuakWifi")
class QuakWifi(ft.Service):
    """Joins a Wi-Fi network for this app only and routes the app's traffic over it (Android 10+)."""

    async def join(self, ssid: str, password: str, timeout: int = 120) -> dict[str, Any]:
        """{joined, bound}. The phone asks the user to confirm; timeout covers that prompt. It's long because a
        phone can take over 30 s to see a camera network that has only just come up."""
        return await self._invoke_method(
            "join", {"ssid": ssid, "password": password, "timeout": timeout}, timeout=timeout + 30
        )

    async def release(self) -> bool:
        return await self._invoke_method("release", {}, timeout=30)

    async def status(self) -> dict[str, Any]:
        return await self._invoke_method("status", {}, timeout=15)
