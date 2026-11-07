"""Keep private data out of logs: passwords, stream keys, network names, the camera's serial, this PC's name.

Every byte Quak logs passes through here, so the Diagnostics screen, its exported file and `quak -v` are all
covered. A hidden message keeps its type and length, so a log stays useful for debugging.
"""

from __future__ import annotations

from quak.core import codec
from quak.core import registry as R

# Protobuf messages whose body is private, by (feature, action)
PRIVATE_PROTOBUF = {
    (R.Feature.NETWORK, R.Action.CONNECT),  # network name
    (R.Feature.NETWORK, R.Action.CONNECT_NEW),  # network name and password
    (R.Feature.COMMAND, R.Action.LIVESTREAM_MODE),  # stream URL, which holds the stream key
    (R.Feature.PAIRING, R.Action.PAIRING_FINISH),  # this PC's name
    (R.Feature.NETWORK, R.response_action(R.Action.GET_AP_ENTRIES)),  # the names of nearby networks
}
STATUS_QUERIES = (R.Query.STATUSES, R.Query.STATUS_PUSH)


def _hide_string_statuses(msg: bytes) -> bytes:
    """A status reply is [query][result][id, length, value...]: blank the values that are network names."""
    out = bytearray(msg[:2])
    i = 2
    while i < len(msg):
        if i + 2 > len(msg) or i + 2 + msg[i + 1] > len(msg):
            return msg[:2]  # not what it looked like: hide the rest rather than guess
        sid, size = msg[i], msg[i + 1]
        out += bytes([sid, 0]) if sid in R.STRING_STATUSES else msg[i : i + 2 + size]
        i += 2 + size
    return bytes(out)


def message(direction: str, uuid: str, msg: bytes) -> tuple[bytes, int]:
    """The part of `msg` that is safe to log, and how many bytes were left out."""
    if len(msg) < 2:
        return msg, 0
    if msg[0] in R.PROTOBUF_FEATURES and (msg[0], msg[1]) in PRIVATE_PROTOBUF:
        safe = msg[:2]
    elif direction == "rx" and uuid == R.CQ_COMMAND_RESP and msg[0] == R.Cmd.HARDWARE_INFO:
        safe = msg[:2]  # holds the serial number
    elif direction == "rx" and uuid == R.CQ_QUERY_RESP and msg[0] in STATUS_QUERIES:
        safe = _hide_string_statuses(msg)
    else:
        return msg, 0
    return safe, len(msg) - len(safe)


def hex_text(direction: str, uuid: str, msg: bytes) -> str:
    """A message as logged: its length header and bytes, in hex, with the private part left out."""
    safe, hidden = message(direction, uuid, msg)
    text = (codec.header(len(msg)) + safe).hex(" ")
    return f"{text} [{hidden} bytes hidden]" if hidden else text
