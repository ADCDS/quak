"""Our protobuf codec vs GoPro's official generated classes (open_gopro, test-only dependency)."""

import importlib
import random
import unittest

from quak.core import proto

try:
    from google.protobuf.descriptor import FieldDescriptor as FD

    PB = {
        name: importlib.import_module(f"open_gopro.models.proto.{name}_pb2")
        for name in (
            "network_management",
            "set_camera_control_status",
            "turbo_transfer",
            "request_get_preset_status",
            "preset_status",
            "live_streaming",
            "response_generic",
        )
    }
except ImportError:  # SDK not installed: cross-checks are skipped
    PB = None


def repeated(f) -> bool:
    flag = getattr(f, "is_repeated", None)
    if flag is None:
        return f.label == FD.LABEL_REPEATED
    return flag() if callable(flag) else bool(flag)


def sdk_class(message: str):
    for mod in PB.values():
        if hasattr(mod, message):
            return getattr(mod, message)
    if message == "ScanEntry":
        return PB["network_management"].ResponseGetApEntries.ScanEntry
    raise KeyError(message)


@unittest.skipIf(PB is None, "open_gopro not installed")
class AgainstSdk(unittest.TestCase):
    REQUESTS = {
        "RequestPairingFinish": {"result": 0, "phoneName": "laptop é"},
        "RequestSetCameraControlStatus": {"camera_control_status": 2},
        "RequestSetTurboActive": {"active": False},
        "RequestGetPresetStatus": {
            "register_preset_status": [1, 2],
            "unregister_preset_status": [],
            "include_hidden": True,
        },
        "RequestGetLiveStreamStatus": {"register_live_stream_status": [1, 2, 3, 4]},
        "RequestSetLiveStreamMode": {
            "url": "rtmp://10.42.0.1:1935/live/tést",
            "encode": False,
            "window_size": 7,
            "minimum_bitrate": 800,
            "maximum_bitrate": 8000,
            "starting_bitrate": 4000,
            "lens": 0,
        },
        "RequestStartScan": {},
        "RequestGetApEntries": {"start_index": 0, "max_entries": 100, "scan_id": 3},
        "RequestConnect": {"ssid": "lab"},
        "RequestConnectNew": {"ssid": "lab", "password": "secret", "bypass_eula_check": True},
        "RequestReleaseNetwork": {},
    }

    def test_requests_serialize_identically(self):
        for name, values in self.REQUESTS.items():
            sdk = sdk_class(name)(**values).SerializeToString()
            self.assertEqual(proto.encode(name, values), sdk, name)

    def test_responses_decode_like_sdk(self):
        nm, ls, ps = PB["network_management"], PB["live_streaming"], PB["preset_status"]
        entry = nm.ResponseGetApEntries.ScanEntry
        samples = [
            ("ResponseGeneric", PB["response_generic"].ResponseGeneric(result=3)),
            ("ResponseStartScanning", nm.ResponseStartScanning(result=1, scanning_state=2)),
            (
                "NotifStartScanning",
                nm.NotifStartScanning(scanning_state=5, scan_id=7, total_entries=2, total_configured_ssid=1),
            ),
            (
                "ResponseGetApEntries",
                nm.ResponseGetApEntries(
                    result=1,
                    scan_id=7,
                    entries=[
                        entry(ssid="lab", signal_strength_bars=3, signal_frequency_mhz=2437, scan_entry_flags=3),
                        entry(ssid="other", signal_strength_bars=1, signal_frequency_mhz=5180, scan_entry_flags=1),
                    ],
                ),
            ),
            ("ResponseConnectNew", nm.ResponseConnectNew(result=1, provisioning_state=2, timeout_seconds=30)),
            ("NotifProvisioningState", nm.NotifProvisioningState(provisioning_state=5)),
            (
                "NotifyLiveStreamStatus",
                ls.NotifyLiveStreamStatus(
                    live_stream_status=3,
                    live_stream_error=0,
                    live_stream_bitrate=4100,
                    live_stream_window_size_supported_array=[4, 7, 12],
                    live_stream_lens_supported_array=[0, 3, 4],
                ),
            ),
            (
                "NotifyPresetStatus",
                ps.NotifyPresetStatus(
                    preset_group_array=[
                        ps.PresetGroup(
                            id=1000,
                            can_add_preset=True,
                            mode_array=[12, 13],
                            preset_array=[
                                ps.Preset(
                                    id=0,
                                    mode=12,
                                    title_id=1,
                                    is_visible=True,
                                    setting_array=[ps.PresetSetting(id=2, value=100)],
                                ),
                                ps.Preset(id=1, mode=-1, title_id=0, custom_name="Mine"),
                            ],
                        )
                    ]
                ),
            ),
        ]
        for name, msg in samples:
            self.assertEqual(proto.decode(name, msg.SerializeToString()), self._to_dict(msg), name)

    def _to_dict(self, msg) -> dict:
        out = {f.name: [] for f in msg.DESCRIPTOR.fields if repeated(f)}
        for f, value in msg.ListFields():
            conv = (lambda v: self._to_dict(v)) if f.type == FD.TYPE_MESSAGE else (lambda v: v)
            out[f.name] = [conv(v) for v in value] if repeated(f) else conv(value)
        return out

    def test_schemas_match_sdk_descriptors(self):
        kinds = {
            FD.TYPE_INT32: "int",
            FD.TYPE_ENUM: "int",
            FD.TYPE_UINT32: "int",
            FD.TYPE_BOOL: "bool",
            FD.TYPE_STRING: "string",
            FD.TYPE_BYTES: "bytes",
        }
        for name, schema in proto.SCHEMAS.items():
            d = sdk_class(name).DESCRIPTOR
            sdk = {
                f.number: (f.name, f.message_type.name if f.type == FD.TYPE_MESSAGE else kinds[f.type], repeated(f))
                for f in d.fields
            }
            self.assertEqual(schema, sdk, name)

    def test_enum_tables_match_sdk(self):
        nm, ls = PB["network_management"], PB["live_streaming"]

        def values(enum):
            return {v.number: v.name for v in enum.DESCRIPTOR.values}

        self.assertEqual(proto.RESULT, values(PB["response_generic"].EnumResultGeneric))
        self.assertEqual({k: "PROVISIONING_" + v for k, v in proto.PROVISIONING.items()}, values(nm.EnumProvisioning))
        self.assertEqual({k: "SCANNING_" + v for k, v in proto.SCANNING.items()}, values(nm.EnumScanning))
        self.assertEqual(
            {k: "LIVE_STREAM_STATE_" + v for k, v in proto.LIVE_STREAM_STATE.items()}, values(ls.EnumLiveStreamStatus)
        )
        self.assertEqual({v: f"WINDOW_SIZE_{k}" for k, v in proto.WINDOW_SIZE.items()}, values(ls.EnumWindowSize))


class Standalone(unittest.TestCase):
    def test_varints_and_negative_enums(self):
        self.assertEqual(proto.encode("PresetSetting", {"id": 300}), bytes.fromhex("08ac02"))
        neg = proto.encode("Preset", {"mode": -1})
        self.assertEqual(len(neg), 11)  # tag + 10-byte varint
        self.assertEqual(proto.decode("Preset", neg)["mode"], -1)

    def test_zero_and_false_are_written(self):
        self.assertEqual(proto.encode("RequestSetTurboActive", {"active": False}), bytes.fromhex("0800"))
        self.assertEqual(
            proto.encode("RequestGetApEntries", {"start_index": 0, "max_entries": 1, "scan_id": 0}),
            bytes.fromhex("080010011800"),
        )

    def test_packed_repeated_and_unknown_fields(self):
        packed = bytes.fromhex("2a03040712")  # field 5, packed varints 4, 7, 18
        unknown = bytes.fromhex("f80701") + bytes.fromhex("0a0161")  # field 127 varint, then field 1 "a"
        self.assertEqual(
            proto.decode("NotifyLiveStreamStatus", packed)["live_stream_window_size_supported_array"], [4, 7, 18]
        )
        self.assertEqual(proto.decode("RequestConnect", unknown), {"ssid": "a"})

    def test_unknown_key_rejected(self):
        with self.assertRaises(KeyError):
            proto.encode("RequestConnect", {"sid": "x"})

    def test_round_trip_fuzz(self):
        rng = random.Random(5)
        for _ in range(200):
            v = {
                "url": "".join(chr(rng.randrange(32, 0x2FF)) for _ in range(rng.randrange(20))),
                "encode": rng.random() < 0.5,
                "window_size": rng.choice([4, 7, 12]),
                "minimum_bitrate": rng.randrange(1 << 31),
                "lens": rng.randrange(-5, 5),
            }
            self.assertEqual(proto.decode("RequestSetLiveStreamMode", proto.encode("RequestSetLiveStreamMode", v)), v)


if __name__ == "__main__":
    unittest.main()
