"""A small protobuf codec for the Open GoPro messages Quak uses.

Only what these messages need: varint (int32/enum/bool), length-delimited (string, bytes,
nested message) and repeated fields. The decoder also accepts packed repeated varints and
skips unknown fields. Every field present in the input dict is written, even when it is 0
or False, because several Open GoPro messages are proto2 with required fields.

Schemas mirror GoPro's .proto files; tests/test_proto.py cross-checks them against the
official generated classes.
"""

from __future__ import annotations

# kinds: "int" (int32/uint32/enum), "bool", "string", "bytes", or a message name (nested)
# schema: {field_number: (name, kind, repeated)}
SCHEMAS: dict[str, dict[int, tuple[str, str, bool]]] = {
    "RequestPairingFinish": {1: ("result", "int", False), 2: ("phoneName", "string", False)},
    "ResponseGeneric": {1: ("result", "int", False)},
    "RequestSetCameraControlStatus": {1: ("camera_control_status", "int", False)},
    "RequestSetTurboActive": {1: ("active", "bool", False)},
    "RequestGetPresetStatus": {
        1: ("register_preset_status", "int", True),
        2: ("unregister_preset_status", "int", True),
        3: ("use_constant_setting_ids", "bool", False),
        4: ("include_hidden", "bool", False),
    },
    "NotifyPresetStatus": {1: ("preset_group_array", "PresetGroup", True)},
    "PresetGroup": {
        1: ("id", "int", False),
        2: ("preset_array", "Preset", True),
        3: ("can_add_preset", "bool", False),
        4: ("icon", "int", False),
        5: ("mode_array", "int", True),
    },
    "Preset": {
        1: ("id", "int", False),
        2: ("mode", "int", False),
        3: ("title_id", "int", False),
        4: ("title_number", "int", False),
        5: ("user_defined", "bool", False),
        6: ("icon", "int", False),
        7: ("setting_array", "PresetSetting", True),
        8: ("is_modified", "bool", False),
        9: ("is_fixed", "bool", False),
        10: ("custom_name", "string", False),
        11: ("is_visible", "bool", False),
    },
    "PresetSetting": {1: ("id", "int", False), 2: ("value", "int", False), 3: ("is_caption", "bool", False)},
    "RequestSetLiveStreamMode": {
        1: ("url", "string", False),
        2: ("encode", "bool", False),
        3: ("window_size", "int", False),
        6: ("cert", "bytes", False),
        7: ("minimum_bitrate", "int", False),
        8: ("maximum_bitrate", "int", False),
        9: ("starting_bitrate", "int", False),
        10: ("lens", "int", False),
    },
    "RequestGetLiveStreamStatus": {
        1: ("register_live_stream_status", "int", True),
        2: ("unregister_live_stream_status", "int", True),
    },
    "NotifyLiveStreamStatus": {
        1: ("live_stream_status", "int", False),
        2: ("live_stream_error", "int", False),
        3: ("live_stream_encode", "bool", False),
        4: ("live_stream_bitrate", "int", False),
        5: ("live_stream_window_size_supported_array", "int", True),
        6: ("live_stream_encode_supported", "bool", False),
        7: ("live_stream_max_lens_unsupported", "bool", False),
        8: ("live_stream_minimum_stream_bitrate", "int", False),
        9: ("live_stream_maximum_stream_bitrate", "int", False),
        10: ("live_stream_lens_supported", "bool", False),
        11: ("live_stream_lens_supported_array", "int", True),
        13: ("live_stream_protune_supported", "bool", False),
    },
    "RequestStartScan": {},
    "ResponseStartScanning": {1: ("result", "int", False), 2: ("scanning_state", "int", False)},
    "NotifStartScanning": {
        1: ("scanning_state", "int", False),
        2: ("scan_id", "int", False),
        3: ("total_entries", "int", False),
        4: ("total_configured_ssid", "int", False),
    },
    "RequestGetApEntries": {
        1: ("start_index", "int", False),
        2: ("max_entries", "int", False),
        3: ("scan_id", "int", False),
    },
    "ResponseGetApEntries": {
        1: ("result", "int", False),
        2: ("scan_id", "int", False),
        3: ("entries", "ScanEntry", True),
    },
    "ScanEntry": {
        1: ("ssid", "string", False),
        2: ("signal_strength_bars", "int", False),
        4: ("signal_frequency_mhz", "int", False),
        5: ("scan_entry_flags", "int", False),
    },
    "RequestConnect": {1: ("ssid", "string", False)},
    "ResponseConnect": {
        1: ("result", "int", False),
        2: ("provisioning_state", "int", False),
        3: ("timeout_seconds", "int", False),
    },
    "RequestConnectNew": {
        1: ("ssid", "string", False),
        2: ("password", "string", False),
        3: ("static_ip", "bytes", False),
        4: ("gateway", "bytes", False),
        5: ("subnet", "bytes", False),
        6: ("dns_primary", "bytes", False),
        7: ("dns_secondary", "bytes", False),
        10: ("bypass_eula_check", "bool", False),
    },
    "ResponseConnectNew": {
        1: ("result", "int", False),
        2: ("provisioning_state", "int", False),
        3: ("timeout_seconds", "int", False),
    },
    "NotifProvisioningState": {1: ("provisioning_state", "int", False)},
    "RequestReleaseNetwork": {},
}

# Enum values used by Quak (names as in GoPro's .proto files)
RESULT = {
    0: "RESULT_UNKNOWN",
    1: "RESULT_SUCCESS",
    2: "RESULT_ILL_FORMED",
    3: "RESULT_NOT_SUPPORTED",
    4: "RESULT_ARGUMENT_OUT_OF_BOUNDS",
    5: "RESULT_ARGUMENT_INVALID",
    6: "RESULT_RESOURCE_NOT_AVAILABLE",
}
RESULT_SUCCESS, RESULT_NOT_SUPPORTED = 1, 3
SCANNING = {
    0: "UNKNOWN",
    1: "NEVER_STARTED",
    2: "STARTED",
    3: "ABORTED_BY_SYSTEM",
    4: "CANCELLED_BY_USER",
    5: "SUCCESS",
}
SCANNING_SUCCESS = 5
PROVISIONING = {
    0: "UNKNOWN",
    1: "NEVER_STARTED",
    2: "STARTED",
    3: "ABORTED_BY_SYSTEM",
    4: "CANCELLED_BY_USER",
    5: "SUCCESS_NEW_AP",
    6: "SUCCESS_OLD_AP",
    7: "ERROR_FAILED_TO_ASSOCIATE",
    8: "ERROR_PASSWORD_AUTH",
    9: "ERROR_EULA_BLOCKING",
    10: "ERROR_NO_INTERNET",
    11: "ERROR_UNSUPPORTED_TYPE",
}
PROVISIONING_DONE = {5, 6}
SCAN_FLAG_AUTHENTICATED, SCAN_FLAG_CONFIGURED, SCAN_FLAG_ASSOCIATED = 0x01, 0x02, 0x08
LIVE_STREAM_STATE = {
    0: "IDLE",
    1: "CONFIG",
    2: "READY",
    3: "STREAMING",
    4: "COMPLETE_STAY_ON",
    5: "FAILED_STAY_ON",
    6: "RECONNECTING",
    7: "UNAVAILABLE",
}
LIVE_STREAM_ERROR = {
    0: "NONE",
    1: "NETWORK",
    2: "CREATESTREAM",
    3: "OUTOFMEMORY",
    4: "INPUTSTREAM",
    5: "INTERNET",
    6: "OSNETWORK",
    7: "SELECTEDNETWORKTIMEOUT",
    8: "SSL_HANDSHAKE",
    9: "CAMERA_BLOCKED",
    10: "UNKNOWN",
    40: "SD_CARD_FULL",
    41: "SD_CARD_REMOVED",
}
WINDOW_SIZE = {480: 4, 720: 7, 1080: 12}
LENS = {"wide": 0, "superview": 3, "linear": 4}
PRESET_GROUP = {1000: "video", 1001: "photo", 1002: "timelapse"}
CAMERA_CONTROL = {"idle": 0, "control": 1, "external": 2}
REGISTER_PRESET_STATUS_PRESET, REGISTER_PRESET_STATUS_PRESET_GROUP_ARRAY = 1, 2
REGISTER_LIVE_STREAM_STATUS = (1, 2, 3, 4)  # status, error, mode, bitrate


# ------------------------------------------------------------------ wire helpers


def _varint(value: int) -> bytes:
    value &= 0xFFFFFFFFFFFFFFFF  # negative int32/enum -> 10-byte two's complement, as protobuf does
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        out.append(b | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _read_varint(data: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        if i >= len(data):
            raise ValueError("truncated varint")
        b = data[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _signed32(v: int) -> int:
    v &= 0xFFFFFFFFFFFFFFFF
    if v >= 1 << 63:
        v -= 1 << 64
    return v


def encode(message: str, values: dict) -> bytes:
    """Serialize a dict as `message`. Unknown keys raise; None values are skipped."""
    schema = SCHEMAS[message]
    by_name = {name: (num, kind, rep) for num, (name, kind, rep) in schema.items()}
    unknown = set(values) - set(by_name)
    if unknown:
        raise KeyError(f"{message} has no fields {sorted(unknown)}")
    out = bytearray()
    for num in sorted(schema):
        name, kind, rep = schema[num]
        if name not in values or values[name] is None:
            continue
        for v in values[name] if rep else [values[name]]:
            if kind in ("int", "bool"):
                out += _varint(num << 3) + _varint(int(v))
            else:
                if kind == "string":
                    payload = v.encode("utf-8")
                elif kind == "bytes":
                    payload = bytes(v)
                else:
                    payload = encode(kind, v)
                out += _varint((num << 3) | 2) + _varint(len(payload)) + payload
    return bytes(out)


def decode(message: str, data: bytes) -> dict:
    """Parse `data` as `message`. Repeated fields come back as lists (empty if absent)."""
    schema = SCHEMAS[message]
    out: dict = {name: [] for name, _, rep in schema.values() if rep}
    i = 0
    while i < len(data):
        tag, i = _read_varint(data, i)
        num, wire = tag >> 3, tag & 7
        field = schema.get(num)
        if wire == 0:
            raw, i = _read_varint(data, i)
            values = [raw]
        elif wire == 2:
            n, i = _read_varint(data, i)
            if i + n > len(data):
                raise ValueError(f"{message}.{num}: truncated")
            chunk, i = data[i : i + n], i + n
            if field and field[1] in ("int", "bool"):  # packed repeated varints
                values, j = [], 0
                while j < len(chunk):
                    v, j = _read_varint(chunk, j)
                    values.append(v)
            else:
                values = [chunk]
        elif wire == 1:
            i += 8
            continue
        elif wire == 5:
            i += 4
            continue
        else:
            raise ValueError(f"{message}: unsupported wire type {wire}")
        if field is None:
            continue
        name, kind, rep = field
        for v in values:
            if kind == "int":
                v = _signed32(v)
            elif kind == "bool":
                v = bool(v)
            elif kind == "string":
                v = bytes(v).decode("utf-8", errors="replace")
            elif kind == "bytes":
                v = bytes(v)
            else:
                v = decode(kind, bytes(v))
            if rep:
                out[name].append(v)
            else:
                out[name] = v
    return out
