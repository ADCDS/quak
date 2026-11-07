import datetime as dt
import unittest

from quak.core import registry as r


class ScheduleCodec(unittest.TestCase):
    def test_known_values(self):
        self.assertEqual(r.encode_schedule(12, 34), 0x0C8B)
        self.assertEqual(r.encode_schedule(16, 48), 0x10C3)
        self.assertEqual(r.describe_schedule(0x0C8B), "12:34")
        self.assertEqual(r.describe_schedule(0), "00:00 (off)")

    def test_round_trip_every_minute(self):
        for h in range(24):
            for m in range(60):
                d = r.decode_schedule(r.encode_schedule(h, m))
                self.assertEqual((d["hour"], d["minute"], d["enabled"], d["is_24h"]), (h, m, True, True))

    def test_rejects_bad_times(self):
        for h, m in [(24, 0), (0, 60), (-1, 0)]:
            with self.assertRaises(ValueError):
                r.encode_schedule(h, m)

    def test_next_schedule(self):
        now = dt.datetime(2026, 11, 5, 10, 0, 10)
        self.assertEqual(r.next_schedule(now), dt.datetime(2026, 11, 5, 10, 2))
        self.assertEqual(r.next_schedule(dt.datetime(2026, 11, 5, 10, 0, 30)), dt.datetime(2026, 11, 5, 10, 2))
        self.assertEqual(r.next_schedule(dt.datetime(2026, 11, 5, 23, 59, 0)), dt.datetime(2026, 11, 6, 0, 1))
        self.assertEqual(r.next_schedule(dt.datetime(2026, 11, 5, 10, 0, 0), 120), dt.datetime(2026, 11, 5, 10, 2))

    def test_parse_schedule_time(self):
        now = dt.datetime(2026, 11, 5, 10, 0, 10)
        self.assertEqual(r.parse_schedule_time("12:34", now), dt.datetime(2026, 11, 5, 12, 34))
        self.assertEqual(r.parse_schedule_time("09:00", now), dt.datetime(2026, 11, 6, 9, 0))
        self.assertEqual(r.parse_schedule_time("+2m", now), dt.datetime(2026, 11, 5, 10, 3))
        for bad in ("25:00", "noon", "+2x"):
            with self.assertRaises(ValueError):
                r.parse_schedule_time(bad, now)


class SettingsTables(unittest.TestCase):
    def test_all_hero11_settings_present(self):
        self.assertEqual(len(r.SETTINGS), 30)
        for sid in (2, 3, 59, 83, 91, 121, 135, 156, 157, 167, 168, 175, 178, 180):
            self.assertIn(sid, r.SETTINGS)

    def test_lookup_by_name(self):
        self.assertEqual(r.setting_id("video_duration"), 156)
        self.assertEqual(r.setting_id("Video Duration"), 156)
        self.assertEqual(r.setting_id("168"), 168)
        with self.assertRaises(KeyError):
            r.setting_id("warp drive")

    def test_parse_values(self):
        self.assertEqual(r.parse_value(175, "pro"), 1)
        self.assertEqual(r.parse_value(175, "Easy"), 0)
        self.assertEqual(r.parse_value(91, "all off"), 4)
        self.assertEqual(r.parse_value(156, "15s"), 1)
        self.assertEqual(r.parse_value(156, "no limit"), 100)
        self.assertEqual(r.parse_value(59, "15 min"), 6)
        self.assertEqual(r.parse_value(2, "5"), 5)  # raw numbers pass through
        with self.assertRaises(ValueError):
            r.parse_value(91, "disco")

    def test_durations(self):
        self.assertEqual(r.duration_value("15s"), 1)
        self.assertEqual(r.duration_value("1m"), 3)
        self.assertEqual(r.duration_value("1 hour"), 7)
        self.assertEqual(r.duration_value("3h"), 9)
        self.assertEqual(r.duration_value("no limit"), 100)
        self.assertEqual(r.duration_seconds(1), 15)
        self.assertIsNone(r.duration_seconds(100))
        with self.assertRaises(ValueError):
            r.duration_value("5s")  # HERO11 has no 5-second duration

    def test_labels_and_sizes(self):
        self.assertEqual(r.value_label(156, 100), "No Limit")
        self.assertEqual(r.value_label(168, 0x0C8B), "12:34")
        self.assertEqual(r.encode_setting_value(168, 0x0C8B), bytes.fromhex("00000c8b"))
        self.assertEqual(r.encode_setting_value(156, 1), b"\x01")


class Statuses(unittest.TestCase):
    def test_names_and_types(self):
        self.assertEqual(r.status_name(39), "videos")
        self.assertEqual(r.decode_status_value(30, b"HERO11\0"), "HERO11")
        self.assertEqual(r.decode_status_value(107, bytes.fromhex("00010000")), 0x10000)
        self.assertEqual(r.preset_group_of(0x10000), "photo")
        self.assertEqual(r.preset_group_of(0x2), "video")

    def test_protobuf_channels(self):
        self.assertEqual(r.PROTOBUF_CHANNEL[r.Feature.QUERY], r.CQ_QUERY)
        self.assertEqual(r.response_action(r.Action.PRESET_STATUS), 0xF2)
        self.assertEqual(r.response_action(r.Action.CAMERA_CONTROL), 0xE9)


if __name__ == "__main__":
    unittest.main()
