"""The transport interface every platform adapter implements (bleak on Linux/Windows,
the Flet bridge on Android, and the in-process simulator)."""

from __future__ import annotations

from typing import Callable, Protocol

Notify = Callable[[str, bytes], None]  # (characteristic uuid, packet)


class Link(Protocol):
    address: str
    on_disconnect: Callable[[], None] | None

    @property
    def is_connected(self) -> bool: ...

    async def connect(self) -> bool:
        """Connect and make sure the link is bonded. Returns True when a NEW bond was made
        (the caller must then send GoPro's pairing-finished message)."""

    async def disconnect(self) -> None: ...

    async def write(self, uuid: str, data: bytes) -> None: ...

    async def read(self, uuid: str) -> bytes: ...

    async def start_notify(self, uuid: str, callback: Notify) -> None: ...
