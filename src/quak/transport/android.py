"""Phone link: the Link interface on top of the quak_native QuakBle Flet service (Android).

The protocol code above (session, camera, flows) is the same as on Linux/Windows; only the
radio calls go through the Flutter plugin.
"""

from __future__ import annotations

from typing import Callable

from quak.core import codec
from quak.core.errors import NOT_BONDED_HINT, Disconnected, NotBonded, NotFound

try:
    from quak_native import QuakBle
except ImportError as e:  # not a phone build
    raise ImportError("quak_native (the phone's Bluetooth bridge) isn't installed") from e


def gopros_in(devices: list[dict]) -> list[dict]:
    """The GoPros among scan results: [{address, name, rssi, advert: codec.Advert}]"""
    return [dict(d, advert=codec.decode_advert(bytes.fromhex(d["gopro"]))) for d in devices if d.get("gopro")]


async def scan_gopros(ble: "QuakBle", seconds: float = 6.0) -> list[dict]:
    return gopros_in(await ble.scan(seconds))


class AndroidLink:
    def __init__(
        self, ble: "QuakBle", address: str, *, wait_pairing: bool = False, log: Callable[[str], None] = lambda s: None
    ) -> None:
        self.ble, self.address, self.wait_pairing, self.log = ble, address.upper(), wait_pairing, log
        self.on_disconnect: Callable[[], None] | None = None
        self._connected = False
        self._callbacks: dict[str, Callable[[str, bytes], None]] = {}
        ble.on_notify = self._notify
        ble.on_disconnected = self._dropped

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> bool:
        state = await self.ble.permissions()
        self.log(f"bluetooth: {state}")
        seen = {d["address"].upper(): d for d in await scan_gopros(self.ble, 8)}
        if self.address not in seen:
            raise NotFound(f"camera {self.address} not seen (is it on and nearby?)")
        advert = seen[self.address]["advert"]
        paired = bool(await self.ble.is_paired(self.address))
        if not paired and not (advert and advert.pairing_mode):
            raise NotBonded(NOT_BONDED_HINT)
        info = await self.ble.connect(self.address)
        self.log(f"connected, MTU {info.get('mtu')}")
        self._connected = True
        if not paired:
            await self.ble.pair(self.address)
            return True
        return False

    def _notify(self, e) -> None:
        cb = self._callbacks.get(e.uuid.lower())
        if cb:
            cb(e.uuid.lower(), bytes.fromhex(e.value))

    def _dropped(self, e) -> None:
        if self._connected:
            self._connected = False
            if self.on_disconnect:
                self.on_disconnect()

    async def disconnect(self) -> None:
        if self._connected:
            self._connected = False
            await self.ble.disconnect()

    async def write(self, uuid: str, data: bytes) -> None:
        if not self._connected:
            raise Disconnected("not connected")
        await self.ble.write(uuid, data.hex())

    async def read(self, uuid: str) -> bytes:
        return bytes.fromhex(await self.ble.read(uuid))

    async def start_notify(self, uuid: str, callback) -> None:
        self._callbacks[uuid.lower()] = callback
        await self.ble.subscribe(uuid)
