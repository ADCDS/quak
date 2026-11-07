"""Linux/BlueZ helpers over D-Bus: device state and Just Works pairing with our own agent.

Ported from the prototype that first bonded a HERO11 (legacy/gopro_schedule.py).
"""

from __future__ import annotations

import asyncio

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method


def dev_path(address: str, hci: str) -> str:
    return f"/org/bluez/{hci}/dev_{address.upper().replace(':', '_')}"


async def system_bus() -> MessageBus:
    return await MessageBus(bus_type=BusType.SYSTEM).connect()


async def dbus_call(bus, path, iface, member, sig="", body=None, timeout=45):
    msg = Message(destination="org.bluez", path=path, interface=iface, member=member, signature=sig, body=body or [])
    reply = await asyncio.wait_for(bus.call(msg), timeout)
    if reply.message_type == MessageType.ERROR:
        raise RuntimeError(f"{reply.error_name}: {reply.body[0] if reply.body else ''}")
    return reply.body


async def device_props(bus, address: str, hci: str) -> dict:
    try:
        body = await dbus_call(
            bus, dev_path(address, hci), "org.freedesktop.DBus.Properties", "GetAll", "s", ["org.bluez.Device1"]
        )
    except Exception as e:  # unknown to bluetoothd
        return {"error": str(e)}
    keep = ("AddressType", "Paired", "Bonded", "Trusted", "Connected", "ServicesResolved", "RSSI", "Blocked")
    return {k: v.value for k, v in body[0].items() if k in keep}


class AutoAgent(ServiceInterface):
    """org.bluez.Agent1 accepting Just Works / confirmation requests (GoPros use Just Works)."""

    def __init__(self, log):
        super().__init__("org.bluez.Agent1")
        self._log = log

    @method()
    def Release(self):
        self._log("agent: release")

    @method()
    def RequestPinCode(self, device: "o") -> "s":  # noqa: F821
        raise RuntimeError("PIN pairing is not supported")

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):  # noqa: F821
        self._log("agent: PIN shown on camera")

    @method()
    def RequestPasskey(self, device: "o") -> "u":  # noqa: F821
        raise RuntimeError("passkey pairing is not supported")

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):  # noqa: F821
        self._log("agent: passkey displayed")

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: F821
        self._log("agent: confirming pairing")

    @method()
    def RequestAuthorization(self, device: "o"):  # noqa: F821
        self._log("agent: authorizing Just Works pairing")

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: F821
        self._log(f"agent: authorizing service {uuid}")

    @method()
    def Cancel(self):
        self._log("agent: pairing cancelled by bluetoothd")


async def pair(bus, address: str, hci: str, log, io_cap: str = "NoInputNoOutput") -> None:
    """Pair from this D-Bus connection so bluetoothd uses our agent, then mark the device trusted."""
    agent_path = "/quak/agent"
    bus.export(agent_path, AutoAgent(log))
    await dbus_call(bus, "/org/bluez", "org.bluez.AgentManager1", "RegisterAgent", "os", [agent_path, io_cap])
    try:
        path = dev_path(address, hci)
        try:
            await dbus_call(bus, path, "org.bluez.Device1", "Pair", timeout=60)
        except RuntimeError as e:
            if "AlreadyExists" not in str(e):
                raise
        await dbus_call(
            bus,
            path,
            "org.freedesktop.DBus.Properties",
            "Set",
            "ssv",
            ["org.bluez.Device1", "Trusted", Variant("b", True)],
        )
    finally:
        try:
            await dbus_call(bus, "/org/bluez", "org.bluez.AgentManager1", "UnregisterAgent", "o", [agent_path])
        except Exception:
            pass
