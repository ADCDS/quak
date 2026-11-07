import asyncio
import unittest

from quak.core import codec
from quak.core import registry as R
from quak.core.errors import Disconnected, Rejected, Timeout
from quak.session import Session, message_key


class ScriptedLink:
    """A fake BLE link: `responder(uuid, msg)` returns [(resp_uuid, msg, delay_s), ...]."""

    def __init__(self, responder, packet_size=20):
        self.responder = responder
        self.packet_size = packet_size
        self.on_disconnect = None
        self.callbacks = {}
        self._connected = True
        self._rx = {u: codec.Reassembler() for u in R.REQUEST_TO_RESPONSE}
        self.received = []

    @property
    def is_connected(self):
        return self._connected

    async def connect(self):
        return False

    async def disconnect(self):
        self._connected = False

    async def write(self, uuid, data):
        if not self._connected:
            raise Disconnected("down")
        msg = self._rx[uuid].feed(data)
        if msg is not None:
            self.received.append((uuid, msg))
            for resp_uuid, reply, delay in self.responder(uuid, msg) or []:
                asyncio.get_running_loop().call_later(delay, self.send, resp_uuid, reply)

    def send(self, uuid, msg):
        for p in codec.frame(msg, self.packet_size):
            if uuid in self.callbacks:
                self.callbacks[uuid](uuid, p)

    async def read(self, uuid):
        return b""

    async def start_notify(self, uuid, cb):
        self.callbacks[uuid] = cb

    def drop(self):
        self._connected = False
        if self.on_disconnect:
            self.on_disconnect()


def ready_responder(extra=None):
    def respond(uuid, msg):
        if uuid == R.CQ_QUERY and msg[0] == 0x13 and list(msg[1:]) == [82]:
            return [(R.CQ_QUERY_RESP, bytes.fromhex("13005201 01"), 0.001)]
        if uuid == R.CQ_SETTINGS and msg[0] == 0x5B:
            return [(R.CQ_SETTINGS_RESP, bytes([0x5B, 0]), 0.001)]
        return extra(uuid, msg) if extra else None

    return respond


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def start(self, extra=None, **kw):
        link = ScriptedLink(ready_responder(extra), **kw)
        s = Session(link, timeout=0.5, keepalive_interval=None)
        await s.start()
        return link, s

    async def test_fragmented_and_delayed_reply(self):
        big = bytes([0x12, 0]) + b"".join(bytes([i, 1, i]) for i in range(1, 60))  # ~180 bytes, 10 packets
        link, s = await self.start(lambda u, m: [(R.CQ_QUERY_RESP, big, 0.05)] if m[0] == 0x12 else None)
        body = await s.query(0x12)
        self.assertEqual(len(codec.parse_tlv(body)), 59)

    async def test_push_reaches_subscriber_while_request_pending(self):
        def extra(uuid, msg):
            if msg[0] == 0x12:
                return [
                    (R.CQ_QUERY_RESP, bytes.fromhex("93 00 0a 01 01"), 0.001),  # push first
                    (R.CQ_QUERY_RESP, bytes.fromhex("12 00 9c 01 64"), 0.02),
                ]

        link, s = await self.start(extra)
        q = s.subscribe((R.CQ_QUERY_RESP, 0x93))
        body = await s.query(0x12, [156])
        self.assertEqual(body, bytes.fromhex("9c 01 64"))
        key, msg = q.get_nowait()
        self.assertEqual(msg, bytes.fromhex("93 00 0a 01 01"))

    async def test_keepalive_reply_not_taken_for_setting_reply(self):
        def extra(uuid, msg):
            if uuid == R.CQ_SETTINGS and msg[0] == 168:
                return [
                    (R.CQ_SETTINGS_RESP, bytes([0x5B, 0]), 0.001),  # stray keep-alive ack
                    (R.CQ_SETTINGS_RESP, bytes([168, 1]), 0.02),
                ]

        link, s = await self.start(extra)
        self.assertEqual(await s.write_setting(168, b"\x00\x00\x0c\x8b"), 1)

    async def test_keepalive_runs_and_is_acked(self):
        link = ScriptedLink(ready_responder())
        s = Session(link, timeout=0.5, keepalive_interval=0.05)
        await s.start()
        # Close between keep-alives: close() cancels one still waiting for its reply, which says nothing about acks
        # (Windows' coarse timers stretch that wait from 1 ms to about 15). A lost ack keeps the counts apart for good.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5
        while s.keepalive_acked < 3 or s.keepalive_acked != s.keepalive_sent:
            self.assertLess(loop.time(), deadline, f"sent {s.keepalive_sent}, acked {s.keepalive_acked}")
            await asyncio.sleep(0.001)
        await s.close()
        self.assertGreaterEqual(s.keepalive_acked, 3)
        self.assertEqual(s.keepalive_acked, s.keepalive_sent)

    async def test_timeout_names_channel_and_payload(self):
        link, s = await self.start()
        with self.assertRaises(Timeout) as cm:
            await s.command(0x3C)
        self.assertIn("0072", str(cm.exception))
        self.assertIn("3c", str(cm.exception))

    async def test_disconnect_fails_pending_request_at_once(self):
        link, s = await self.start()
        task = asyncio.create_task(s.command(0x3C, timeout=10))
        await asyncio.sleep(0.02)
        link.drop()
        with self.assertRaises(Disconnected):
            await asyncio.wait_for(task, 1)
        with self.assertRaises(Disconnected):
            await s.query(0x13, [8])

    async def test_command_status_raises(self):
        link, s = await self.start(lambda u, m: [(R.CQ_COMMAND_RESP, bytes([m[0], 2]), 0.001)])
        with self.assertRaises(Rejected):
            await s.command(0x01, b"\x01")

    async def test_cleanups_run_on_close_even_after_errors(self):
        link, s = await self.start()
        ran = []

        async def ok():
            ran.append("ok")

        async def bad():
            raise RuntimeError("boom")

        s.add_cleanup(ok)
        s.add_cleanup(bad)
        await s.close()
        self.assertEqual(ran, ["ok"])
        self.assertFalse(link.is_connected)

    async def test_protobuf_reply_matched_by_feature_and_action(self):
        def extra(uuid, msg):
            if uuid == R.CQ_COMMAND and msg[:2] == bytes([0xF1, 0x6B]):
                return [
                    (R.CQ_COMMAND_RESP, bytes.fromhex("f1 e9 08 01"), 0.001),  # a different reply
                    (R.CQ_COMMAND_RESP, bytes.fromhex("f1 eb 08 01"), 0.02),
                ]

        link, s = await self.start(extra)
        r = await s.protobuf(0xF1, 0x6B, "RequestSetTurboActive", {"active": True}, "ResponseGeneric")
        self.assertEqual(r, {"result": 1})

    def test_message_keys(self):
        self.assertEqual(message_key(R.CQ_QUERY_RESP, bytes.fromhex("f5 f2 00")), (R.CQ_QUERY_RESP, 0xF5, 0xF2))
        self.assertEqual(message_key(R.CQ_QUERY_RESP, bytes.fromhex("13 00")), (R.CQ_QUERY_RESP, 0x13))
        self.assertEqual(message_key(R.NET_MGMT_RESP, bytes.fromhex("02 0b")), (R.NET_MGMT_RESP, 0x02, 0x0B))


if __name__ == "__main__":
    unittest.main()
