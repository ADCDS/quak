"""Real-camera link over bleak (Linux/BlueZ and Windows/WinRT)."""

from __future__ import annotations

import asyncio
import sys
from typing import Callable

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from quak.core import codec
from quak.core.errors import BLUETOOTH_LOST, NOT_BONDED_HINT, Disconnected, NotBonded, NotFound


async def scan_cameras(seconds: float = 8.0, hci: str = "hci0") -> list[dict]:
    """GoPro cameras advertising nearby: address, name, and whether each is on its pairing screen."""
    found: dict[str, dict] = {}

    def seen(dev, adv):
        mfr = adv.manufacturer_data.get(codec.GOPRO_COMPANY_ID)
        info = codec.decode_advert(mfr) if mfr else None
        if info:
            found[dev.address] = {
                "address": dev.address,
                "name": dev.name or adv.local_name or dev.address,
                "pairing": info.pairing_mode,
            }

    kwargs = {"adapter": hci} if sys.platform.startswith("linux") else {}
    scanner = BleakScanner(seen, scanning_mode="active", **kwargs)
    try:
        await scanner.start()
        await asyncio.sleep(seconds)
        await scanner.stop()
    except BleakError as e:
        raise Disconnected(BLUETOOTH_LOST) from e
    return list(found.values())


class BleakLink:
    def __init__(
        self,
        address: str,
        *,
        hci: str = "hci0",
        scan_timeout: float = 45.0,
        connect_timeout: float = 20.0,
        retries: int = 3,
        wait_pairing: bool = False,
        log: Callable[[str], None] = lambda s: None,
    ) -> None:
        self.address = address.upper()
        self.hci = hci
        self.scan_timeout, self.connect_timeout, self.retries = scan_timeout, connect_timeout, retries
        self.wait_pairing = wait_pairing
        self.log = log
        self.on_disconnect: Callable[[], None] | None = None
        self.advert: codec.Advert | None = None
        self._client: BleakClient | None = None
        self._bus = None

    @property
    def is_connected(self) -> bool:
        return bool(self._client and self._client.is_connected)

    async def scan(self):
        found = asyncio.Event()
        state: dict = {}

        def seen(dev, adv):
            if dev.address.upper() != self.address:
                return
            mfr = adv.manufacturer_data.get(codec.GOPRO_COMPANY_ID)
            info = codec.decode_advert(mfr) if mfr else None
            if info != state.get("advert"):
                self.log(f"advertisement: {info}")
            state.update(device=dev, advert=info)
            if not self.wait_pairing or (info and info.pairing_mode):
                found.set()

        kwargs = {"adapter": self.hci} if sys.platform.startswith("linux") else {}
        scanner = BleakScanner(seen, scanning_mode="active", **kwargs)
        for attempt in range(6):  # some USB dongles crash and re-enumerate: the adapter is busy or missing meanwhile
            try:
                await scanner.start()
                break
            except BleakError as e:
                if not any(sign in str(e) for sign in ("InProgress", "NotReady", "not found")):
                    raise
                if attempt == 5:
                    raise Disconnected(BLUETOOTH_LOST) from e
                self.log("the Bluetooth adapter is busy; retrying the scan")
                await asyncio.sleep(1.0 + attempt)
        try:
            await asyncio.wait_for(found.wait(), self.scan_timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            try:
                await scanner.stop()
            except BleakError as e:  # the adapter vanished under the scan
                raise Disconnected(BLUETOOTH_LOST) from e
        if "device" not in state:
            raise NotFound(f"camera {self.address} not seen within {self.scan_timeout:.0f} s (is it on and nearby?)")
        self.advert = state.get("advert")
        return state["device"]

    async def _bonded(self) -> bool:
        if not sys.platform.startswith("linux"):
            return True  # Windows: pairing state isn't exposed; try and let pair() sort it out
        from quak.transport import bluez

        self._bus = self._bus or await bluez.system_bus()
        props = await bluez.device_props(self._bus, self.address, self.hci)
        return bool(props.get("Paired") or props.get("Bonded"))

    async def connect(self) -> bool:
        device = await self.scan()
        bonded = await self._bonded()
        if not bonded and not (self.advert and self.advert.pairing_mode):
            raise NotBonded(NOT_BONDED_HINT)
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            self._client = BleakClient(
                device, timeout=self.connect_timeout, disconnected_callback=lambda _c: self._dropped()
            )
            try:
                self.log(f"connecting ({attempt}/{self.retries})")
                await self._client.connect()
                break
            except (BleakError, asyncio.TimeoutError, EOFError) as e:
                last = e
                self.log(f"connect failed: {type(e).__name__}: {e}")
                await asyncio.sleep(2)
        else:
            raise Disconnected(f"could not connect to {self.address}: {last}")
        if bonded and sys.platform.startswith("linux"):
            return False
        return await self._pair()

    async def _pair(self) -> bool:
        if sys.platform.startswith("linux"):
            from quak.transport import bluez

            await bluez.pair(self._bus, self.address, self.hci, self.log)
        else:
            await self._client.pair()
        return True

    def _dropped(self) -> None:
        if self.on_disconnect:
            self.on_disconnect()

    async def disconnect(self) -> None:
        if self._client and self._client.is_connected:
            await self._client.disconnect()
        if self._bus:
            self._bus.disconnect()
            self._bus = None

    async def write(self, uuid: str, data: bytes) -> None:
        if not self.is_connected:
            raise Disconnected("not connected")
        await self._client.write_gatt_char(uuid, data, response=True)

    async def read(self, uuid: str) -> bytes:
        return bytes(await self._client.read_gatt_char(uuid))

    async def start_notify(self, uuid: str, callback) -> None:
        try:
            await self._client.start_notify(uuid, lambda _char, data: callback(uuid, bytes(data)))
        except BleakError as e:  # the link or the adapter dropped before the services resolved
            raise Disconnected(BLUETOOTH_LOST) from e
