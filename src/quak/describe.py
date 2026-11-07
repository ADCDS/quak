"""Human-readable meaning of a raw message, for the Diagnostics log."""

from __future__ import annotations

from quak.core import codec, proto
from quak.core import registry as R

CMD_NAMES = {
    R.Cmd.SHUTTER: "Shutter",
    R.Cmd.SLEEP: "Sleep",
    R.Cmd.SET_DATETIME: "Set date/time",
    R.Cmd.GET_DATETIME: "Read date/time",
    R.Cmd.SET_LOCAL_DATETIME: "Set local date/time",
    R.Cmd.GET_LOCAL_DATETIME: "Read local date/time",
    R.Cmd.AP_CONTROL: "Camera Wi-Fi",
    R.Cmd.HILIGHT: "HiLight",
    R.Cmd.HARDWARE_INFO: "Hardware info",
    R.Cmd.LOAD_PRESET_GROUP: "Load preset group",
    R.Cmd.LOAD_PRESET: "Load preset",
}
QUERY_NAMES = {
    R.Query.SETTINGS: "Read settings",
    R.Query.STATUSES: "Read statuses",
    R.Query.CAPABILITIES: "Allowed values",
    R.Query.REGISTER_STATUSES: "Watch statuses",
    R.Query.REGISTER_SETTINGS: "Watch settings",
    R.Query.UNREGISTER_STATUSES: "Stop watching statuses",
    R.Query.STATUS_PUSH: "Status changed",
    R.Query.SETTING_PUSH: "Setting changed",
}
PROTO_NAMES = {
    (R.Feature.PAIRING, R.Action.PAIRING_FINISH): "Pairing finished",
    (R.Feature.NETWORK, R.Action.SCAN_NETWORKS): "Wi-Fi scan",
    (R.Feature.NETWORK, R.Action.GET_AP_ENTRIES): "Wi-Fi scan results",
    (R.Feature.NETWORK, R.Action.CONNECT): "Join remembered network",
    (R.Feature.NETWORK, R.Action.CONNECT_NEW): "Join new network",
    (R.Feature.NETWORK, R.Action.NOTIF_SCAN): "Wi-Fi scan progress",
    (R.Feature.NETWORK, R.Action.NOTIF_PROVISIONING): "Network join progress",
    (R.Feature.COMMAND, R.Action.CAMERA_CONTROL): "Camera control",
    (R.Feature.COMMAND, R.Action.TURBO): "Turbo transfer",
    (R.Feature.COMMAND, R.Action.LIVESTREAM_MODE): "Livestream setup",
    (R.Feature.COMMAND, R.Action.RELEASE_NETWORK): "Leave network",
    (R.Feature.QUERY, R.Action.PRESET_STATUS): "Presets",
    (R.Feature.QUERY, R.Action.PRESET_MODIFIED): "Preset changed",
    (R.Feature.QUERY, R.Action.LIVESTREAM_STATUS): "Livestream status",
    (R.Feature.QUERY, R.Action.LIVESTREAM_NOTIF): "Livestream status changed",
}


def category(uuid: str, msg: bytes) -> str:
    if uuid in (R.CQ_SETTINGS, R.CQ_SETTINGS_RESP):
        return (
            "keepalive"
            if msg[:1] == bytes([R.KEEP_ALIVE_SETTING]) and msg[-1:] in (b"\x42", b"\x00") and len(msg) <= 3
            else "settings"
        )
    if uuid in (R.CQ_QUERY, R.CQ_QUERY_RESP):
        return "queries"
    if uuid in (R.NET_MGMT, R.NET_MGMT_RESP):
        return "network"
    return "commands"


def describe(direction: str, uuid: str, msg: bytes) -> str:
    try:
        return _describe(direction, uuid, msg)
    except Exception:  # never let a log line break the app
        return ""


def _describe(direction: str, uuid: str, msg: bytes) -> str:
    if not msg:
        return ""
    reply = direction == "rx"
    if uuid in (R.CQ_SETTINGS, R.CQ_SETTINGS_RESP):
        sid = msg[0]
        if reply:
            status = R.TLV_STATUS.get(msg[1], msg[1]) if len(msg) > 1 else "?"
            return (
                "Keep-alive OK"
                if sid == R.KEEP_ALIVE_SETTING and msg[1:2] == b"\x00"
                else f"{R.setting_name(sid)}: {status}"
            )
        value = codec.to_int(msg[2 : 2 + msg[1]]) if len(msg) > 2 else 0
        if sid == R.KEEP_ALIVE_SETTING and value == R.KEEP_ALIVE_VALUE:
            return "Keep-alive"
        return f"Set {R.setting_name(sid)} = {R.value_label(sid, value)}"
    if msg[0] in R.PROTOBUF_FEATURES and len(msg) > 1 and (uuid in (R.NET_MGMT, R.NET_MGMT_RESP) or msg[0] >= 0xF1):
        action = msg[1] & 0x7F if reply and msg[1] & 0x80 and (msg[0], msg[1]) not in PROTO_NAMES else msg[1]
        name = PROTO_NAMES.get(
            (msg[0], action), PROTO_NAMES.get((msg[0], msg[1]), f"protobuf {msg[0]:02x} {msg[1]:02x}")
        )
        if reply and len(msg) > 2 and msg[1] & 0x80 and (msg[0], msg[1]) not in PROTO_NAMES:
            try:
                result = proto.decode("ResponseGeneric", msg[2:]).get("result")
                return f"{name}: {proto.RESULT.get(result, result).removeprefix('RESULT_').lower()}"
            except ValueError:
                pass
        return name
    if uuid in (R.CQ_QUERY, R.CQ_QUERY_RESP):
        name = QUERY_NAMES.get(msg[0], f"query 0x{msg[0]:02x}")
        if not reply:
            ids = list(msg[1:])
            return (
                f"{name} ({len(ids)})"
                if len(ids) > 3
                else f"{name} "
                + ", ".join(
                    R.setting_name(i) if msg[0] in (R.Query.SETTINGS, R.Query.CAPABILITIES) else R.status_name(i)
                    for i in ids
                )
            )
        if msg[0] in (R.Query.STATUS_PUSH, R.Query.STATUSES):
            pairs = codec.parse_tlv(msg[2:])
            if len(pairs) <= 3:
                return f"{name}: " + ", ".join(f"{R.status_name(k)} = {R.decode_status_value(k, v)}" for k, v in pairs)
            return f"{name}: {len(pairs)} values"
        return f"{name}: {R.TLV_STATUS.get(msg[1], msg[1]) if len(msg) > 1 else ''}"
    name = CMD_NAMES.get(msg[0], f"command 0x{msg[0]:02x}")
    if reply:
        return f"{name}: {R.TLV_STATUS.get(msg[1], msg[1]) if len(msg) > 1 else ''}"
    if msg[0] == R.Cmd.SHUTTER and len(msg) > 2:
        return f"{name} {'on' if msg[2] else 'off'}"
    if msg[0] == R.Cmd.AP_CONTROL and len(msg) > 2:
        return f"{name} {'on' if msg[2] else 'off'}"
    return name
