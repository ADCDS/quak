"""When the adapter fails mid-connect the user gets a clear Quak error, not a raw bleak exception."""

import unittest
from unittest import mock

from bleak.exc import BleakCharacteristicNotFoundError, BleakDBusError, BleakError

from quak.core.errors import BLUETOOTH_LOST, Disconnected
from quak.transport.ble import BleakLink


def dbus_error(name):
    return BleakDBusError(f"org.bluez.Error.{name}", [])


class AdapterFailureTests(unittest.IsolatedAsyncioTestCase):
    def link(self):
        return BleakLink("CAMERA", scan_timeout=0.01)

    def scanner(self, **methods):
        scanner = mock.Mock(start=mock.AsyncMock(), stop=mock.AsyncMock())
        for name, effect in methods.items():
            getattr(scanner, name).side_effect = effect
        patcher = mock.patch("quak.transport.ble.BleakScanner", return_value=scanner)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleep = mock.patch("quak.transport.ble.asyncio.sleep", mock.AsyncMock())
        sleep.start()
        self.addCleanup(sleep.stop)
        return scanner

    async def test_adapter_that_stays_unavailable(self):
        scanner = self.scanner(start=dbus_error("NotReady"))
        with self.assertRaisesRegex(Disconnected, BLUETOOTH_LOST):
            await self.link().scan()
        self.assertEqual(scanner.start.await_count, 6)

    async def test_adapter_missing_while_it_restarts(self):
        scanner = self.scanner(start=BleakError("adapter 'hci0' not found"))
        with self.assertRaisesRegex(Disconnected, BLUETOOTH_LOST):
            await self.link().scan()
        self.assertEqual(scanner.start.await_count, 6)

    async def test_adapter_vanishing_during_the_scan(self):
        self.scanner(stop=dbus_error("Failed"))
        with self.assertRaisesRegex(Disconnected, BLUETOOTH_LOST):
            await self.link().scan()

    async def test_other_scan_errors_are_not_relabelled(self):
        self.scanner(start=dbus_error("NotPermitted"))
        with self.assertRaises(BleakDBusError):
            await self.link().scan()

    async def test_services_gone_after_connecting(self):
        link = self.link()
        link._client = mock.Mock(start_notify=mock.AsyncMock(side_effect=BleakCharacteristicNotFoundError("uuid")))
        with self.assertRaisesRegex(Disconnected, BLUETOOTH_LOST):
            await link.start_notify("uuid", lambda *_: None)


if __name__ == "__main__":
    unittest.main()
