"""AndroidLink (the phone's Link) driven against the simulated camera through a fake QuakBle bridge.

The fake speaks the bridge's contract: bytes as hex strings, notifications as events with
.uuid and .value, scan results as dicts carrying the GoPro manufacturer data in hex.
"""

import asyncio
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "extensions" / "quak_native" / "src"))
try:
    import quak_native  # noqa: F401  (needs flet)
except ImportError:
    fake = types.ModuleType("quak_native")
    fake.QuakBle = object
    sys.modules["quak_native"] = fake

from quak.connect import Target, open_camera  # noqa: E402
from quak.core.errors import NotBonded, NotFound  # noqa: E402
from quak.transport.android import AndroidLink, scan_gopros  # noqa: E402
from quak.transport.sim import SimCamera, SimLink  # noqa: E402

CAMERA = "AA:BB:CC:DD:EE:FF"
OTHER = "11:22:33:44:55:66"


class FakeBridge:
    def __init__(self, cam: SimCamera) -> None:
        self.cam = cam
        self.link = SimLink(cam, CAMERA)
        self.on_notify = self.on_disconnected = None
        self.calls: list[str] = []

    async def permissions(self) -> str:
        self.calls.append("permissions")
        return "poweredOn"

    async def scan(self, seconds: float) -> list[dict]:
        self.calls.append("scan")
        flags = 0x01 | (0x04 if self.cam.pairing_mode else 0)
        return [
            {"address": CAMERA.lower(), "name": "GoPro Test", "rssi": -50, "gopro": bytes([2, flags, 58, 0x0F]).hex()},
            {"address": OTHER, "name": "Headphones", "rssi": -70, "gopro": ""},
        ]

    async def is_paired(self, address: str) -> bool:
        return self.cam.bonded

    async def connect(self, address: str) -> dict:
        self.calls.append("connect")
        await self.link.connect()  # bonds when the camera is on its pairing screen
        self.link.on_disconnect = lambda: self.on_disconnected(SimpleNamespace(address=address))
        return {"mtu": 247}

    async def pair(self, address: str) -> bool:
        self.calls.append("pair")
        return True

    async def disconnect(self) -> bool:
        self.calls.append("disconnect")
        await self.link.disconnect()
        return True

    async def write(self, uuid: str, data: str) -> bool:
        await self.link.write(uuid, bytes.fromhex(data))
        return True

    async def read(self, uuid: str) -> str:
        return (await self.link.read(uuid)).hex()

    async def subscribe(self, uuid: str) -> bool:
        await self.link.start_notify(uuid, lambda u, d: self.on_notify(SimpleNamespace(uuid=u.upper(), value=d.hex())))
        return True


def target(bridge: FakeBridge, address: str = CAMERA) -> Target:
    return Target(
        address=address,
        keepalive=None,
        client_name="test",
        link_factory=lambda t, a: AndroidLink(bridge, a, wait_pairing=t.wait_pairing),
    )


class AndroidLinkTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cam = SimCamera(latency=0.0005)
        self.bridge = FakeBridge(self.cam)

    async def asyncTearDown(self):
        await self.cam.close()

    async def test_scan_keeps_only_gopros(self):
        found = await scan_gopros(self.bridge, 1)
        self.assertEqual([d["address"] for d in found], [CAMERA.lower()])
        self.assertFalse(found[0]["advert"].pairing_mode)

    async def test_bonded_session_runs_commands(self):
        async with open_camera(target(self.bridge)) as cam:
            self.assertFalse(cam.new_bond)
            info = await cam.hardware_info()
            self.assertIn("HERO11", info.model_name)
            await cam.sync_clock()
            await cam.shutter(True)
            await cam.shutter(False)
        self.assertNotIn("pair", self.bridge.calls)
        self.assertEqual(self.bridge.calls[-1], "disconnect")

    async def test_new_bond_on_pairing_screen(self):
        self.cam.bonded, self.cam.pairing_mode = False, True
        async with open_camera(target(self.bridge)) as cam:
            self.assertTrue(cam.new_bond)
        self.assertIn("pair", self.bridge.calls)

    async def test_not_bonded_and_not_pairing(self):
        self.cam.bonded = False
        with self.assertRaises(NotBonded):
            async with open_camera(target(self.bridge)):
                pass
        self.assertNotIn("connect", self.bridge.calls)

    async def test_camera_not_seen(self):
        with self.assertRaises(NotFound):
            async with open_camera(target(self.bridge, OTHER)):
                pass

    async def test_camera_drop_reaches_the_session(self):
        async with open_camera(target(self.bridge)) as cam:
            self.cam.press_power_button()
            await asyncio.wait_for(cam.s.disconnected.wait(), 2)
            self.assertFalse(cam.link.is_connected)


if __name__ == "__main__":
    unittest.main()
