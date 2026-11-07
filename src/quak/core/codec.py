"""GoPro BLE wire format: packet framing, TLV messages, dates, advertisements. Pure, no I/O.

Spec: https://gopro.github.io/OpenGoPro/docs/ble/protocol/data_protocol
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

PACKET_SIZE = 20  # GoPro packets are at most 20 bytes, whatever the negotiated MTU


# ------------------------------------------------------------------ framing


def header(n: int) -> bytes:
    """The length header in front of an n-byte message."""
    if n < 0x20:
        return bytes([n])
    if n < 0x2000:
        return bytes([0x20 | (n >> 8), n & 0xFF])
    if n <= 0xFFFF:
        return bytes([0x40, n >> 8, n & 0xFF])
    raise ValueError(f"message too long ({n} bytes)")


def frame(payload: bytes, packet_size: int = PACKET_SIZE) -> list[bytes]:
    """Split one message into BLE packets: a header packet plus continuation packets."""
    data = header(len(payload)) + payload
    packets = [data[:packet_size]]
    rest, counter = data[packet_size:], 0
    while rest:
        packets.append(bytes([0x80 | (counter & 0x0F)]) + rest[: packet_size - 1])
        rest, counter = rest[packet_size - 1 :], counter + 1
    return packets


class Reassembler:
    """Rebuilds messages from the packets of ONE characteristic.

    Broken sequences (a continuation without a start, a counter gap, a reserved header
    type, a new start before the previous message completed) are dropped and counted.
    """

    def __init__(self) -> None:
        self._buf: bytearray | None = None
        self._need = 0
        self._next = 0
        self.dropped = 0

    def _reset(self) -> None:
        self._buf, self._need, self._next = None, 0, 0

    def feed(self, packet: bytes) -> bytes | None:
        """Add one packet; return the completed message, or None."""
        if not packet:
            return None
        head = packet[0]
        if head & 0x80:  # continuation
            if self._buf is None or (head & 0x0F) != self._next:
                self.dropped += 1
                self._reset()
                return None
            self._next = (self._next + 1) & 0x0F
            self._buf += packet[1:]
        else:
            if self._buf is not None:  # previous message never completed
                self.dropped += 1
            kind = (head >> 5) & 0x03
            if kind == 0:
                need, body = head & 0x1F, packet[1:]
            elif kind == 1 and len(packet) >= 2:
                need, body = ((head & 0x1F) << 8) | packet[1], packet[2:]
            elif kind == 2 and len(packet) >= 3:
                need, body = (packet[1] << 8) | packet[2], packet[3:]
            else:
                self.dropped += 1
                self._reset()
                return None
            self._buf, self._need, self._next = bytearray(body), need, 0
        if len(self._buf) >= self._need:
            msg = bytes(self._buf[: self._need])
            self._reset()
            return msg
        return None


# ------------------------------------------------------------------ TLV


def command(cmd_id: int, *params: bytes) -> bytes:
    """TLV command: id, then each parameter as length + value."""
    return bytes([cmd_id]) + b"".join(bytes([len(p)]) + p for p in params)


def setting(setting_id: int, value: bytes) -> bytes:
    return bytes([setting_id, len(value)]) + value


def query(query_id: int, ids: tuple[int, ...] | list[int] = ()) -> bytes:
    return bytes([query_id, *ids])


def protobuf(feature: int, action: int, body: bytes = b"") -> bytes:
    return bytes([feature, action]) + body


def parse_tlv(data: bytes) -> list[tuple[int, bytes]]:
    """Parse id/length/value triples (query replies and pushes)."""
    out, i = [], 0
    while i < len(data):
        if i + 2 > len(data):
            raise ValueError(f"truncated TLV at byte {i}: {data.hex(' ')}")
        key, n = data[i], data[i + 1]
        if i + 2 + n > len(data):
            raise ValueError(f"TLV {key} wants {n} bytes, {len(data) - i - 2} left")
        out.append((key, bytes(data[i + 2 : i + 2 + n])))
        i += 2 + n
    return out


def to_int(value: bytes) -> int:
    return int.from_bytes(value, "big")


def from_int(value: int, size: int) -> bytes:
    return value.to_bytes(size, "big")


# ------------------------------------------------------------------ dates


def encode_datetime(t: dt.datetime, tz_minutes: int | None = None, dst: bool | None = None) -> bytes:
    """Date/time parameter for 0x0D (no tz) or 0x0F (with UTC offset in minutes and DST)."""
    data = t.year.to_bytes(2, "big") + bytes([t.month, t.day, t.hour, t.minute, t.second])
    if tz_minutes is not None:
        data += tz_minutes.to_bytes(2, "big", signed=True) + bytes([1 if dst else 0])
    return data


def decode_datetime(data: bytes) -> tuple[dt.datetime, int | None, bool | None]:
    """Decode the value of a 0x0E / 0x10 reply. Returns (naive local time, tz minutes, dst)."""
    if len(data) < 7:
        raise ValueError(f"date too short: {data.hex(' ')}")
    t = dt.datetime(int.from_bytes(data[:2], "big"), data[2], data[3], data[4], data[5], data[6])
    if len(data) >= 10:
        return t, int.from_bytes(data[7:9], "big", signed=True), bool(data[9])
    return t, None, None


# ------------------------------------------------------------------ hardware info


@dataclass(frozen=True)
class HardwareInfo:
    model_number: int
    model_name: str
    firmware: str
    serial: str
    ap_ssid: str
    ap_mac: str


def decode_hardware_info(body: bytes) -> HardwareInfo:
    """Body of the 0x3C reply after id and status: length-prefixed fields; extra trailing fields ignored."""
    fields, i = [], 0
    while i < len(body) and len(fields) < 7:
        n = body[i]
        fields.append(bytes(body[i + 1 : i + 1 + n]))
        i += 1 + n
    if len(fields) < 7:
        raise ValueError(f"hardware info has {len(fields)} fields, expected 7")
    text = [f.decode("utf-8", errors="replace") for f in fields]
    return HardwareInfo(
        model_number=to_int(fields[0]),
        model_name=text[1],
        firmware=text[3],
        serial=text[4],
        ap_ssid=text[5],
        ap_mac=text[6],
    )


# ------------------------------------------------------------------ advertisement

MODELS = {
    55: "HERO9 Black",
    57: "HERO10 Black",
    58: "HERO11 Black",
    60: "HERO11 Black Mini",
    62: "HERO12 Black",
    65: "HERO13 Black",
}
GOPRO_COMPANY_ID = 0x02F2


@dataclass(frozen=True)
class Advert:
    schema: int
    model_id: int
    awake: bool
    wifi_ap: bool
    pairing_mode: bool
    central_role: bool
    new_media: bool
    capabilities: int

    @property
    def model(self) -> str:
        return MODELS.get(self.model_id, f"model {self.model_id}")


def decode_advert(mfr: bytes) -> Advert | None:
    """Decode manufacturer data for company 0x02F2 (schema 2/3)."""
    if len(mfr) < 4:
        return None
    st = mfr[1]
    return Advert(
        schema=mfr[0],
        model_id=mfr[2],
        awake=bool(st & 0x01),
        wifi_ap=bool(st & 0x02),
        pairing_mode=bool(st & 0x04),
        central_role=bool(st & 0x08),
        new_media=bool(st & 0x10),
        capabilities=mfr[3],
    )
