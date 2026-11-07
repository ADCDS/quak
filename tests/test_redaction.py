"""Private data must never reach a log: not the Diagnostics screen, its exported file, `quak -v` or an error."""

import unittest
from unittest import mock

from quak.connect import Target
from quak.core import codec, redact
from quak.core import registry as R
from quak.core.errors import Timeout
from quak.service import CameraService
from quak.transport.sim import SimCamera

PASSWORD = "dummy-wifi-password-Xy7"
SSID = "dummy-home-network"
STREAM_URL = "rtmp://example.invalid/live/dummy-stream-key-Zq9"


def appears_in(secret: str, text: str) -> bool:
    raw = secret.encode()
    return secret in text or raw.hex(" ") in text or raw.hex() in text


class RedactMessageTests(unittest.TestCase):
    def test_private_protobuf_bodies_are_hidden(self):
        for feature, action in redact.PRIVATE_PROTOBUF:
            msg = bytes([feature, action]) + PASSWORD.encode()
            self.assertEqual(redact.message("tx", R.NET_MGMT, msg), (bytes([feature, action]), len(PASSWORD)))

    def test_other_messages_are_untouched(self):
        keep_alive = bytes.fromhex("5b0142")
        self.assertEqual(redact.message("tx", R.CQ_SETTINGS, keep_alive), (keep_alive, 0))

    def test_hardware_info_reply_hides_the_serial(self):
        msg = bytes([R.Cmd.HARDWARE_INFO, 0]) + b"SERIAL123456"
        self.assertEqual(redact.message("rx", R.CQ_COMMAND_RESP, msg), (msg[:2], 12))

    def test_status_reply_blanks_network_names_and_keeps_the_rest(self):
        ssid = SSID.encode()
        msg = bytes([R.Query.STATUSES, 0, 8, 1, 0, 29, len(ssid)]) + ssid + bytes([10, 1, 1])
        safe, hidden = redact.message("rx", R.CQ_QUERY_RESP, msg)
        self.assertEqual(hidden, len(ssid))
        self.assertEqual(safe, bytes([R.Query.STATUSES, 0, 8, 1, 0, 29, 0, 10, 1, 1]))

    def test_a_malformed_status_reply_hides_everything_after_the_result(self):
        msg = bytes([R.Query.STATUSES, 0, 29, 200]) + SSID.encode()
        self.assertEqual(redact.message("rx", R.CQ_QUERY_RESP, msg)[0], msg[:2])

    def test_the_text_says_what_was_hidden(self):
        msg = bytes([R.Feature.NETWORK, R.Action.CONNECT_NEW]) + PASSWORD.encode()
        text = redact.hex_text("tx", R.NET_MGMT, msg)
        self.assertIn(f"[{len(PASSWORD)} bytes hidden]", text)
        self.assertFalse(appears_in(PASSWORD, text))


class EndToEndTests(unittest.IsolatedAsyncioTestCase):
    """Through the real session and service, against the simulated camera."""

    async def asyncSetUp(self):
        self.sim = SimCamera(latency=0.0005)
        self.svc = CameraService(Target(sim=True, sim_camera=self.sim, keepalive=None), auto_reconnect=False)
        await self.svc.connect()

    async def asyncTearDown(self):
        await self.svc.disconnect()
        await self.sim.close()

    def log_text(self) -> str:
        return "\n".join(f"{l.hex} {l.meaning}" for l in self.svc.log)

    async def test_join_and_livestream_setup_stay_out_of_the_log(self):
        cam = self.svc.cam
        with self.assertRaises(Exception):  # the simulated camera can't see this network; the request still goes out
            await cam.join_network(SSID, PASSWORD, False, timeout=0.2)
        await cam.set_livestream_mode(STREAM_URL)
        text = self.log_text()
        for secret in (PASSWORD, SSID, STREAM_URL, "dummy-stream-key-Zq9"):
            self.assertFalse(appears_in(secret, text), secret)
        self.assertIn("Join new network", text)  # the log still says what happened
        self.assertIn("bytes hidden", text)

    async def test_the_serial_stays_out_of_the_log(self):
        info = await self.svc.cam.hardware_info()
        self.assertTrue(info.serial)
        self.assertFalse(appears_in(info.serial, self.log_text()))

    async def test_a_timeout_message_hides_the_password(self):
        session = self.svc.cam.s
        msg = codec.protobuf(R.Feature.NETWORK, R.Action.CONNECT_NEW, PASSWORD.encode())
        with mock.patch.object(session.link, "write", mock.AsyncMock()), self.assertRaises(Timeout) as caught:
            await session.request(R.NET_MGMT, msg, ("nobody", "answers"), timeout=0.01)
        self.assertFalse(appears_in(PASSWORD, str(caught.exception)))


if __name__ == "__main__":
    unittest.main()
