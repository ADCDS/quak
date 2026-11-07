"""Diagnostics shows each message as it goes over the air: length header first."""

import unittest

from quak.core import codec, redact
from quak.core import registry as R


class ByteLogTests(unittest.TestCase):
    def text(self, message: bytes) -> str:
        return redact.hex_text("tx", R.CQ_SETTINGS, message)

    def test_short_message_has_its_length_byte(self):
        self.assertEqual(self.text(bytes.fromhex("5b0142")), "03 5b 01 42")

    def test_long_message_has_the_two_byte_header(self):
        self.assertTrue(self.text(b"\x32" * 40).startswith("20 28 32 32"))

    def test_header_matches_what_frame_sends(self):
        for n in (0, 1, 31, 32, 0x1FFF, 0x2000, 0xFFFF):
            self.assertEqual(codec.frame(bytes(n))[0][: len(codec.header(n))], codec.header(n))
        with self.assertRaises(ValueError):
            codec.header(0x10000)


if __name__ == "__main__":
    unittest.main()
