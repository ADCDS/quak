import datetime as dt
import random
import unittest

from quak.core import codec
from quak.core.codec import Reassembler, frame


def hx(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", ""))


def reassemble(packets: list[bytes]) -> list[bytes]:
    r, out = Reassembler(), []
    for p in packets:
        msg = r.feed(p)
        if msg is not None:
            out.append(msg)
    return out


class FramingTests(unittest.TestCase):
    def test_header_kinds_at_boundaries(self):
        for n, head in [
            (0, "00"),
            (1, "01"),
            (19, "13"),
            (20, "14"),
            (31, "1f"),
            (32, "20 20"),
            (100, "20 64"),
            (8191, "3f ff"),
            (8192, "40 20 00"),
            (65535, "40 ff ff"),
        ]:
            packets = frame(bytes(n))
            self.assertTrue(packets[0].startswith(hx(head)), (n, packets[0][:3].hex()))
            self.assertTrue(all(len(p) <= 20 for p in packets))
            self.assertEqual(reassemble(packets), [bytes(n)], n)
        with self.assertRaises(ValueError):
            frame(bytes(65536))

    def test_continuation_counter_wraps(self):
        packets = frame(bytes(range(256)) * 2)  # 512 bytes -> 28 packets
        counters = [p[0] & 0x0F for p in packets[1:]]
        self.assertEqual(counters[:17], list(range(16)) + [0])
        self.assertEqual(reassemble(packets), [bytes(range(256)) * 2])

    def test_random_round_trips(self):
        rng = random.Random(11)
        for _ in range(300):
            msg = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 600)))
            self.assertEqual(reassemble(frame(msg)), [msg])

    def test_two_characteristics_interleaved(self):
        a, b = bytes(range(70)), bytes(range(100, 150))
        ra, rb, got = Reassembler(), Reassembler(), []
        pa, pb = frame(a), frame(b)
        for i in range(max(len(pa), len(pb))):
            if i < len(pa) and (m := ra.feed(pa[i])):
                got.append(m)
            if i < len(pb) and (m := rb.feed(pb[i])):
                got.append(m)
        self.assertCountEqual(got, [a, b])

    def test_broken_sequences_are_dropped(self):
        r = Reassembler()
        self.assertIsNone(r.feed(hx("80 01 02")))  # continuation without a start
        packets = frame(bytes(60))
        r.feed(packets[0])
        self.assertIsNone(r.feed(packets[2]))  # counter gap
        self.assertIsNone(r.feed(hx("60 00")))  # reserved header type
        self.assertEqual(r.dropped, 3)
        self.assertEqual(reassemble(frame(b"ok")), [b"ok"])  # still usable

    def test_new_start_abandons_unfinished_message(self):
        r = Reassembler()
        r.feed(frame(bytes(40))[0])
        self.assertEqual(r.feed(hx("02 aa bb")), hx("aa bb"))
        self.assertEqual(r.dropped, 1)


class GoldenFrames(unittest.TestCase):
    """Replies captured from a HERO11 Black (firmware 2.20) during the research."""

    def test_status_reply(self):
        msg = reassemble(
            [hx("20 1a 13 00 08 01 00 0a 01 00 13 01 04 52 01 01 6b 04 ff ff"), hx("80 ff ff 6c 01 00 72 01 00")]
        )[0]
        self.assertEqual(msg[:2], hx("13 00"))
        values = {k: codec.to_int(v) for k, v in codec.parse_tlv(msg[2:])}
        self.assertEqual(values, {8: 0, 10: 0, 19: 4, 82: 1, 107: 0xFFFFFFFF, 108: 0, 114: 0})

    def test_settings_reply(self):
        msg = reassemble([hx("20 14 12 00 3b 01 00 9c 01 64 9d 01 64 a8 04 00 00 00 00 af"), hx("80 01 00")])[0]
        values = {k: codec.to_int(v) for k, v in codec.parse_tlv(msg[2:])}
        self.assertEqual(values, {59: 0, 156: 100, 157: 100, 168: 0, 175: 0})

    def test_capabilities_reply_in_easy_controls(self):
        msg = reassemble(
            [
                hx("20 1d 32 00 3b 01 00 3b 01 01 3b 01 04 3b 01 06 3b 01 07 9c"),
                hx("80 00 9d 00 a8 00 af 01 00 af 01 01"),
            ]
        )[0]
        caps: dict[int, list[int]] = {}
        for k, v in codec.parse_tlv(msg[2:]):
            caps.setdefault(k, [])
            if v:
                caps[k].append(codec.to_int(v))
        self.assertEqual(caps, {59: [0, 1, 4, 6, 7], 156: [], 157: [], 168: [], 175: [0, 1]})

    def test_clock_reply(self):
        msg = reassemble([hx("0b 0e 00 08 07 ea 0b 04 09 01 04 00")])[0]
        self.assertEqual(msg[:2], hx("0e 00"))
        t, tz, dst = codec.decode_datetime(msg[3 : 3 + msg[2]])
        self.assertEqual((t, tz, dst), (dt.datetime(2026, 11, 4, 9, 1, 4), None, None))

    def test_setting_write_reply(self):
        self.assertEqual(reassemble([hx("02 a8 00")]), [hx("a8 00")])


class TlvTests(unittest.TestCase):
    def test_command_bytes(self):
        cases = {
            "shutter on": (codec.command(0x01, b"\x01"), "01 01 01"),
            "shutter off": (codec.command(0x01, b"\x00"), "01 01 00"),
            "hilight": (codec.command(0x18), "18"),
            "sleep": (codec.command(0x05), "05"),
            "ap on": (codec.command(0x17, b"\x01"), "17 01 01"),
            "load group 1000": (codec.command(0x3E, (1000).to_bytes(2, "big")), "3e 02 03 e8"),
            "load preset": (codec.command(0x40, (0x10000).to_bytes(4, "big")), "40 04 00 01 00 00"),
            "keep-alive": (codec.setting(0x5B, b"\x42"), "5b 01 42"),
            "schedule": (codec.setting(168, (0x0C8B).to_bytes(4, "big")), "a8 04 00 00 0c 8b"),
        }
        for name, (got, want) in cases.items():
            self.assertEqual(got, hx(want), name)
        # with the length header added by framing, these are the documented packets
        self.assertEqual(frame(codec.command(0x01, b"\x01")), [hx("03 01 01 01")])
        self.assertEqual(frame(codec.setting(0x5B, b"\x42")), [hx("03 5b 01 42")])

    def test_local_datetime_parameter(self):
        p = codec.encode_datetime(dt.datetime(2026, 11, 4, 10, 15, 30), tz_minutes=-180, dst=False)
        self.assertEqual(frame(codec.command(0x0F, p)), [hx("0c 0f 0a 07 ea 0b 04 0a 0f 1e ff 4c 00")])
        self.assertEqual(codec.decode_datetime(p), (dt.datetime(2026, 11, 4, 10, 15, 30), -180, False))
        p = codec.encode_datetime(dt.datetime(2026, 1, 2, 3, 4, 5), tz_minutes=60, dst=True)
        self.assertEqual(codec.decode_datetime(p)[1:], (60, True))

    def test_tlv_errors(self):
        with self.assertRaises(ValueError):
            codec.parse_tlv(hx("08 02 00"))
        with self.assertRaises(ValueError):
            codec.parse_tlv(hx("08"))
        self.assertEqual(codec.parse_tlv(b""), [])

    def test_hardware_info_tolerates_trailing_fields(self):
        def field(b: bytes) -> bytes:
            return bytes([len(b)]) + b

        body = (
            field((58).to_bytes(4, "big"))
            + field(b"HERO11 Black")
            + field(b"0x05")
            + field(b"H22.01.02.20.00")
            + field(b"TESTSERIAL0001")
            + field(b"GP-TEST")
            + field(b"0123456789ab")
            + hx("01 00 01 01")
        )
        info = codec.decode_hardware_info(body)
        self.assertEqual(
            (info.model_number, info.model_name, info.firmware, info.ap_mac),
            (58, "HERO11 Black", "H22.01.02.20.00", "0123456789ab"),
        )

    def test_advert(self):
        a = codec.decode_advert(hx("02 05 3a 33 00 ff ee dd cc bb aa 0f"))
        self.assertEqual(
            (a.schema, a.model, a.awake, a.pairing_mode, a.wifi_ap, a.new_media),
            (2, "HERO11 Black", True, True, False, False),
        )
        self.assertIsNone(codec.decode_advert(b"\x02"))


if __name__ == "__main__":
    unittest.main()
