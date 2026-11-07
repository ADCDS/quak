"""IDs, UUIDs and value helpers for the HERO11 Black. Pure, no I/O."""

from __future__ import annotations

import datetime as dt
import re

from quak.core import tables
from quak.i18n import N_, tr

# ------------------------------------------------------------------ BLE characteristics

_BASE = "b5f9{}-aa8d-11e3-9046-0002a5d5c51b"
WAP_SSID, WAP_PASSWORD, WAP_POWER, WAP_STATE = (_BASE.format(x) for x in ("0002", "0003", "0004", "0005"))
CQ_COMMAND, CQ_COMMAND_RESP = _BASE.format("0072"), _BASE.format("0073")
CQ_SETTINGS, CQ_SETTINGS_RESP = _BASE.format("0074"), _BASE.format("0075")
CQ_QUERY, CQ_QUERY_RESP = _BASE.format("0076"), _BASE.format("0077")
NET_MGMT, NET_MGMT_RESP = _BASE.format("0091"), _BASE.format("0092")
RESPONSE_CHARS = (CQ_COMMAND_RESP, CQ_SETTINGS_RESP, CQ_QUERY_RESP, NET_MGMT_RESP)
REQUEST_TO_RESPONSE = {
    CQ_COMMAND: CQ_COMMAND_RESP,
    CQ_SETTINGS: CQ_SETTINGS_RESP,
    CQ_QUERY: CQ_QUERY_RESP,
    NET_MGMT: NET_MGMT_RESP,
}


def short_uuid(uuid: str) -> str:
    return uuid[4:8]


# ------------------------------------------------------------------ TLV command / query ids


class Cmd:
    SHUTTER = 0x01
    SLEEP = 0x05
    SET_DATETIME = 0x0D
    GET_DATETIME = 0x0E
    SET_LOCAL_DATETIME = 0x0F
    GET_LOCAL_DATETIME = 0x10
    AP_CONTROL = 0x17
    HILIGHT = 0x18
    HARDWARE_INFO = 0x3C
    LOAD_PRESET_GROUP = 0x3E
    LOAD_PRESET = 0x40
    CLIENT_INFO = 0x50
    OPEN_GOPRO_VERSION = 0x51


class Query:
    SETTINGS = 0x12
    STATUSES = 0x13
    CAPABILITIES = 0x32
    REGISTER_SETTINGS = 0x52
    REGISTER_STATUSES = 0x53
    REGISTER_CAPABILITIES = 0x62
    UNREGISTER_SETTINGS = 0x72
    UNREGISTER_STATUSES = 0x73
    UNREGISTER_CAPABILITIES = 0x82
    SETTING_PUSH = 0x92
    STATUS_PUSH = 0x93
    CAPABILITY_PUSH = 0xA2


KEEP_ALIVE_SETTING, KEEP_ALIVE_VALUE = 0x5B, 0x42  # setting 91 (LED) with the special value 0x42
TLV_STATUS = {0: "success", 1: N_("error"), 2: N_("invalid parameter")}

# ------------------------------------------------------------------ protobuf feature / action ids


class Feature:
    NETWORK = 0x02
    PAIRING = 0x03
    COMMAND = 0xF1
    SETTING = 0xF3
    QUERY = 0xF5


PROTOBUF_FEATURES = (Feature.NETWORK, Feature.PAIRING, Feature.COMMAND, Feature.SETTING, Feature.QUERY)


class Action:
    PAIRING_FINISH = 0x01
    SCAN_NETWORKS = 0x02
    GET_AP_ENTRIES = 0x03
    CONNECT = 0x04
    CONNECT_NEW = 0x05
    NOTIF_SCAN = 0x0B
    NOTIF_PROVISIONING = 0x0C
    CAMERA_CONTROL = 0x69
    TURBO = 0x6B
    PRESET_STATUS = 0x72
    PRESET_MODIFIED = 0xF3
    LIVESTREAM_STATUS = 0x74
    LIVESTREAM_NOTIF = 0xF5
    RELEASE_NETWORK = 0x78
    LIVESTREAM_MODE = 0x79


def response_action(action: int) -> int:
    return action | 0x80


# Where each protobuf request goes (its reply arrives on the paired response characteristic)
PROTOBUF_CHANNEL = {
    Feature.NETWORK: NET_MGMT,
    Feature.PAIRING: NET_MGMT,
    Feature.COMMAND: CQ_COMMAND,
    Feature.SETTING: CQ_SETTINGS,
    Feature.QUERY: CQ_QUERY,
}

# ------------------------------------------------------------------ presets

PRESET_GROUPS = {"video": 1000, "photo": 1001, "timelapse": 1002}


def preset_group_of(preset_id: int) -> str:
    """HERO11 preset ids carry their group in the high half: video 0x0009xxxx (seen on a HERO11 with firmware
    2.20; 0x0000xxxx in the Open GoPro examples), photo 0x0001xxxx, time lapse 0x0002xxxx."""
    return {0: "video", 9: "video", 1: "photo", 2: "timelapse"}.get((preset_id >> 16) & 0xFFFF, "other")


# ------------------------------------------------------------------ settings


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


SETTINGS = tables.SETTINGS
SETTING_IDS = {slug(s["name"]): sid for sid, s in SETTINGS.items()}
SCHEDULED_CAPTURE, VIDEO_DURATION, MULTI_SHOT_DURATION, CONTROL_MODE = 168, 156, 157, 175
TESTED_MODEL = "HERO11 Black"


def model_note(model: str) -> str | None:
    """A warning for any camera but the one Quak's settings tables and tests cover."""
    if not model or model == TESTED_MODEL:
        return None
    return tr(
        "this camera is a {model}, and Quak is only tested on a {tested_model}. Settings may be missing or wrong",
        model=model,
        tested_model=TESTED_MODEL,
    )


AUTO_POWER_DOWN, WIRELESS_BAND, HINDSIGHT = 59, 178, 167
PRO_ONLY = {156, 157, 167, 168}  # no allowed values in Easy controls (seen on firmware 2.20)
# Bytes on the wire. The docs call 30/32/168 int64, but the camera sent 168 as 4 bytes (fw 2.20);
# 30/32 are ASSUMED to match. Every other HERO11 setting is one byte.
SETTING_SIZE = {168: 4, 30: 4, 32: 4}


def setting_id(key: str | int) -> int:
    """Accept an id ("156"), a name ("video_duration", "Video Duration")."""
    if isinstance(key, int) or str(key).isdigit():
        sid = int(key)
    else:
        sid = SETTING_IDS.get(slug(str(key)), -1)
    if sid not in SETTINGS and sid != 0x5B:
        raise KeyError(f"unknown HERO11 setting {key!r}")
    return sid


def setting_name(sid: int) -> str:
    return SETTINGS.get(sid, {}).get("name", f"setting {sid}")


def value_label(sid: int, value: int) -> str:
    if sid == SCHEDULED_CAPTURE:
        return describe_schedule(value)
    label = SETTINGS.get(sid, {}).get("options", {}).get(value, str(value))
    return label[:-2] if label.endswith(".0") else label  # frame rates are listed as "30.0"


def parse_value(sid: int, text: str | int) -> int:
    """Accept a number or an option name ("No Limit", "no_limit", "pro", "15s")."""
    if isinstance(text, int) or str(text).lstrip("-").isdigit():
        return int(text)
    if sid in (VIDEO_DURATION, MULTI_SHOT_DURATION):
        return duration_value(str(text), sid)
    wanted = slug(str(text))
    for value, label in SETTINGS.get(sid, {}).get("options", {}).items():
        if slug(label) == wanted:
            return value
    options = ", ".join(SETTINGS.get(sid, {}).get("options", {}).values())
    raise ValueError(f"{setting_name(sid)}: unknown value {text!r} (choices: {options})")


def encode_setting_value(sid: int, value: int) -> bytes:
    return value.to_bytes(SETTING_SIZE.get(sid, 1), "big")


# ------------------------------------------------------------------ durations (156 / 157)

_UNIT = {
    "s": 1,
    "sec": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hour": 3600,
    "hours": 3600,
}


def _seconds_of_label(label: str) -> int | None:
    m = re.fullmatch(r"(\d+)\s*(\w+)", label.strip().lower())
    return int(m.group(1)) * _UNIT[m.group(2)] if m and m.group(2) in _UNIT else None


def duration_choices(sid: int = VIDEO_DURATION) -> dict[int, int | None]:
    """value -> seconds (None = no limit), from the HERO11 option table."""
    out = {}
    for value, label in SETTINGS[sid]["options"].items():
        out[value] = None if slug(label) == "no_limit" else _seconds_of_label(label)
    return out


def duration_value(text: str, sid: int = VIDEO_DURATION) -> int:
    """'15s', '1m', '1 hour', 'no limit' -> setting value."""
    t = text.strip().lower()
    if slug(t) in ("no_limit", "nolimit", "none", "unlimited"):
        secs = None
    else:
        m = re.fullmatch(r"(\d+)\s*([a-z]+)", t)
        if not m or m.group(2) not in _UNIT:
            raise ValueError(f"can't read duration {text!r}; use e.g. 15s, 1m, 1h or 'no limit'")
        secs = int(m.group(1)) * _UNIT[m.group(2)]
    for value, s in duration_choices(sid).items():
        if s == secs:
            return value
    allowed = ", ".join(SETTINGS[sid]["options"].values())
    raise ValueError(f"HERO11 has no {text!r} duration (choices: {allowed})")


def duration_seconds(value: int, sid: int = VIDEO_DURATION) -> int | None:
    return duration_choices(sid).get(value)


# ------------------------------------------------------------------ scheduled capture (168)


def encode_schedule(hour: int, minute: int, enabled: bool = True, is_24h: bool = True) -> int:
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"bad time {hour}:{minute:02d}")
    return (hour << 8) | (minute << 2) | (int(is_24h) << 1) | int(enabled)


def decode_schedule(value: int) -> dict:
    return {
        "enabled": bool(value & 1),
        "is_24h": bool(value & 2),
        "minute": (value >> 2) & 0x3F,
        "hour": (value >> 8) & 0x1F,
    }


def describe_schedule(value: int) -> str:
    s = decode_schedule(value)
    return f"{s['hour']:02d}:{s['minute']:02d}" + ("" if s["enabled"] else " (off)")


def next_schedule(now: dt.datetime, lead_seconds: int = 90) -> dt.datetime:
    """First whole minute at least `lead_seconds` after `now`."""
    t = now + dt.timedelta(seconds=lead_seconds)
    if t.second or t.microsecond:
        t = t.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    return t


def parse_schedule_time(text: str, now: dt.datetime) -> dt.datetime:
    """'HH:MM' (next occurrence) or '+2m' / '+90s' / '+1h' (rounded up to the next whole minute)."""
    t = text.strip().lower()
    m = re.fullmatch(r"\+(\d+)\s*([smh])", t)
    if m:
        lead = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)]
        return next_schedule(now, lead)
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
    if not m:
        raise ValueError(f"can't read time {text!r}; use HH:MM or +2m")
    hour, minute = int(m.group(1)), int(m.group(2))
    encode_schedule(hour, minute)  # validates
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if when <= now:
        when += dt.timedelta(days=1)
    return when


# ------------------------------------------------------------------ statuses

STATUSES = tables.STATUSES
STRING_STATUSES = {29, 30}  # connected Wi-Fi SSID, access point SSID


class Status:
    BATTERY_PRESENT = 1
    BATTERY_BARS = 2
    OVERHEATING = 6
    BUSY = 8
    ENCODING = 10
    ENCODING_DURATION = 13
    WIRELESS_ENABLED = 17
    PAIRING_STATE = 19
    CONNECTED_SSID = 29
    AP_SSID = 30
    PREVIEW_STREAM = 32
    SD_STATUS = 33
    REMAINING_PHOTOS = 34
    REMAINING_VIDEO = 35
    PHOTOS = 38
    VIDEOS = 39
    REMAINING_KB = 54
    AP_MODE = 69
    BATTERY_PERCENT = 70
    READY = 82
    PRESET_GROUP = 96
    PRESET = 97
    SCHEDULE_PRESET = 107
    SCHEDULE_ARMED = 108
    TURBO = 113
    CAMERA_CONTROL = 114


def status_name(status_id: int) -> str:
    return STATUSES.get(status_id, f"status_{status_id}")


def decode_status_value(status_id: int, raw: bytes) -> int | str:
    if status_id in STRING_STATUSES:
        return raw.rstrip(b"\0").decode("utf-8", errors="replace")
    return int.from_bytes(raw, "big") if raw else 0
