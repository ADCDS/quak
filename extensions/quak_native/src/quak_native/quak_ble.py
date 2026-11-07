"""Python side of the QuakBle service (Dart side: src/flutter/quak_native)."""

from dataclasses import dataclass
from typing import Any, Optional

import flet as ft


@dataclass
class QuakBleNotifyEvent(ft.Event["QuakBle"]):
    uuid: str
    value: str  # hex (not `data`: ft.Event already has that field)


@dataclass
class QuakBleDisconnectEvent(ft.Event["QuakBle"]):
    address: str


@ft.control("QuakBle")
class QuakBle(ft.Service):
    """Bluetooth LE central on the phone. Bytes travel as hex strings."""

    on_notify: Optional[ft.EventHandler[QuakBleNotifyEvent]] = None
    on_disconnected: Optional[ft.EventHandler[QuakBleDisconnectEvent]] = None

    async def permissions(self) -> str:
        """Ask for Bluetooth (and location) permission; returns the adapter state."""
        return await self._invoke_method("permissions", {}, timeout=120)

    async def scan(self, seconds: float = 6.0) -> list[dict[str, Any]]:
        """[{address, name, rssi, gopro: manufacturer data hex for company 0x02F2}]"""
        return await self._invoke_method("scan", {"seconds": seconds}, timeout=seconds + 15)

    async def connect(self, address: str, timeout: int = 20) -> dict[str, Any]:
        return await self._invoke_method("connect", {"address": address, "timeout": timeout}, timeout=timeout + 30)

    async def is_paired(self, address: str) -> Optional[bool]:
        return await self._invoke_method("is_paired", {"address": address}, timeout=30)

    async def pair(self, address: str) -> bool:
        return await self._invoke_method("pair", {"address": address}, timeout=90)

    async def disconnect(self) -> bool:
        return await self._invoke_method("disconnect", {}, timeout=20)

    async def write(self, uuid: str, data_hex: str) -> bool:
        return await self._invoke_method("write", {"uuid": uuid, "data": data_hex}, timeout=15)

    async def read(self, uuid: str) -> str:
        return await self._invoke_method("read", {"uuid": uuid}, timeout=15)

    async def subscribe(self, uuid: str) -> bool:
        return await self._invoke_method("subscribe", {"uuid": uuid}, timeout=15)
