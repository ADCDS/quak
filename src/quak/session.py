"""One BLE session with a camera.

- Writes are framed into GoPro packets; notifications are reassembled per characteristic.
- Each reply is matched to its request by a key: (response characteristic, id) for TLV
  messages, (response characteristic, feature, action) for protobuf messages.
- Anything that isn't a pending reply (status pushes, preset/livestream/network
  notifications) goes to subscribers; nothing is dropped.
- One lock serializes requests (write + reply). The keep-alive uses the same path.
- A disconnect fails every pending request at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from quak.core import codec, proto, redact
from quak.core import registry as R
from quak.core.errors import Disconnected, Rejected, Timeout
from quak.describe import category, describe

Key = tuple


@dataclass(frozen=True)
class Trace:
    """A message for logging. `text` is already free of private data; the message itself is not kept."""

    t: float
    direction: str  # "tx" or "rx"
    uuid: str
    text: str
    meaning: str = ""
    category: str = ""


def message_key(uuid: str, msg: bytes) -> Key:
    if not msg:
        return (uuid, None)
    if uuid == R.NET_MGMT_RESP or msg[0] in (R.Feature.COMMAND, R.Feature.SETTING, R.Feature.QUERY):
        return (uuid, msg[0], msg[1] if len(msg) > 1 else None)
    return (uuid, msg[0])


class Session:
    def __init__(
        self,
        link,
        *,
        timeout: float = 5.0,
        keepalive_interval: float | None = 3.0,
        trace: Callable[[Trace], None] | None = None,
    ) -> None:
        self.link = link
        self.timeout = timeout
        self.keepalive_interval = keepalive_interval
        self.trace = trace
        self._rx = {u: codec.Reassembler() for u in R.RESPONSE_CHARS}
        self._pending: dict[Key, asyncio.Future] = {}
        self._subs: dict[Key, list[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()
        self._keepalive: asyncio.Task | None = None
        self._cleanups: list[Callable[[], Awaitable[None]]] = []
        self.closed = False
        self.disconnected = asyncio.Event()
        self.keepalive_sent = self.keepalive_acked = 0
        link.on_disconnect = self._on_disconnect

    # ---------------------------------------------------------------- lifecycle

    async def start(self, *, new_bond: bool = False, client_name: str = "quak", ready_timeout: float = 15.0) -> None:
        for uuid in R.RESPONSE_CHARS:
            await self.link.start_notify(uuid, self._on_packet)
        if new_bond:
            await self.pairing_finish(client_name)
        await self.wait_ready(ready_timeout)
        if self.keepalive_interval:
            self._keepalive = asyncio.create_task(self._keepalive_loop())

    def add_cleanup(self, fn: Callable[[], Awaitable[None]]) -> None:
        """Run `fn` (best effort) when the session closes, last added first."""
        self._cleanups.append(fn)

    async def close(self) -> None:
        if self.closed:
            return
        if self._keepalive:
            self._keepalive.cancel()
            with contextlib.suppress(BaseException):
                await self._keepalive
        if self.link.is_connected:
            for fn in reversed(self._cleanups):
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(fn(), 2.0)
        self.closed = True
        with contextlib.suppress(Exception):
            await self.link.disconnect()
        self._fail_pending(Disconnected("session closed"))

    def _on_disconnect(self) -> None:
        self.disconnected.set()
        self._fail_pending(Disconnected("the camera disconnected"))

    def _fail_pending(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    @property
    def connected(self) -> bool:
        return not self.closed and self.link.is_connected and not self.disconnected.is_set()

    # ---------------------------------------------------------------- receive side

    def _on_packet(self, uuid: str, packet: bytes) -> None:
        r = self._rx.get(uuid)
        if r is None:
            return
        msg = r.feed(bytes(packet))
        if msg is None:
            return
        self._trace("rx", uuid, msg)
        key = message_key(uuid, msg)
        fut = self._pending.pop(key, None)
        if fut is not None and not fut.done():
            fut.set_result(msg)
            return
        for q in self._subs.get(key, []) + self._subs.get(("*",), []):
            q.put_nowait((key, msg))

    def subscribe(self, *keys: Key) -> asyncio.Queue:
        """Queue receiving (key, message) for unsolicited messages with any of `keys`; ("*",) = all."""
        q: asyncio.Queue = asyncio.Queue()
        for k in keys:
            self._subs.setdefault(k, []).append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        for lst in self._subs.values():
            if q in lst:
                lst.remove(q)

    def _trace(self, direction: str, uuid: str, msg: bytes) -> None:
        if self.trace:
            safe, _ = redact.message(direction, uuid, msg)
            self.trace(
                Trace(
                    time.time(),
                    direction,
                    uuid,
                    redact.hex_text(direction, uuid, msg),
                    describe(direction, uuid, safe),
                    category(uuid, safe),
                )
            )

    # ---------------------------------------------------------------- send side

    async def request(self, uuid: str, payload: bytes, key: Key, timeout: float | None = None) -> bytes:
        if not self.connected:
            raise Disconnected("not connected to the camera")
        wait = timeout or self.timeout
        async with self._lock:
            fut = asyncio.get_running_loop().create_future()
            self._pending[key] = fut
            try:
                self._trace("tx", uuid, payload)
                for packet in codec.frame(payload):
                    await self.link.write(uuid, packet)
                return await asyncio.wait_for(fut, wait)
            except asyncio.TimeoutError:
                raise Timeout(
                    f"no reply on {R.short_uuid(uuid)} to {redact.hex_text('tx', uuid, payload)} within {wait:.1f} s "
                    f"(connected: {self.link.is_connected})"
                ) from None
            finally:
                if self._pending.get(key) is fut:
                    del self._pending[key]

    async def command(self, cmd_id: int, *params: bytes, timeout: float | None = None) -> bytes:
        """TLV command; returns the reply after id and status. Non-zero status raises Rejected."""
        msg = await self.request(R.CQ_COMMAND, codec.command(cmd_id, *params), (R.CQ_COMMAND_RESP, cmd_id), timeout)
        status = msg[1] if len(msg) > 1 else 0
        if status:
            raise Rejected(f"command 0x{cmd_id:02x}", R.TLV_STATUS.get(status, status))
        return msg[2:]

    async def query(self, query_id: int, ids=(), timeout: float | None = None) -> bytes:
        msg = await self.request(R.CQ_QUERY, codec.query(query_id, tuple(ids)), (R.CQ_QUERY_RESP, query_id), timeout)
        if len(msg) > 1 and msg[1]:
            raise Rejected(f"query 0x{query_id:02x}", R.TLV_STATUS.get(msg[1], msg[1]))
        return msg[2:]

    async def write_setting(self, setting_id: int, value: bytes, timeout: float | None = None) -> int:
        """Returns the camera's status byte (0 = success)."""
        msg = await self.request(
            R.CQ_SETTINGS, codec.setting(setting_id, value), (R.CQ_SETTINGS_RESP, setting_id), timeout
        )
        return msg[1] if len(msg) > 1 else 0

    async def protobuf(
        self, feature: int, action: int, message: str, values: dict, response: str, timeout: float | None = None
    ) -> dict:
        uuid = R.PROTOBUF_CHANNEL[feature]
        resp_uuid = R.REQUEST_TO_RESPONSE[uuid]
        payload = codec.protobuf(feature, action, proto.encode(message, values))
        msg = await self.request(uuid, payload, (resp_uuid, feature, R.response_action(action)), timeout)
        return proto.decode(response, msg[2:])

    async def read(self, uuid: str) -> bytes:
        if not self.connected:
            raise Disconnected("not connected to the camera")
        return bytes(await self.link.read(uuid))

    # ---------------------------------------------------------------- helpers

    async def pairing_finish(self, name: str) -> None:
        name = name.encode("utf-8")[:60].decode("utf-8", errors="ignore") or "quak"
        r = await self.protobuf(
            R.Feature.PAIRING,
            R.Action.PAIRING_FINISH,
            "RequestPairingFinish",
            {"result": 0, "phoneName": name},
            "ResponseGeneric",
        )
        if r.get("result") != proto.RESULT_SUCCESS:
            raise Rejected("pairing finish", proto.RESULT.get(r.get("result"), r.get("result")))

    async def wait_ready(self, timeout: float = 15.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            body = await self.query(R.Query.STATUSES, [R.Status.READY])
            values = {k: codec.to_int(v) for k, v in codec.parse_tlv(body)}
            if values.get(R.Status.READY, 1) == 1:
                return
            if asyncio.get_running_loop().time() > deadline:
                raise Timeout("the camera never reported ready")
            await asyncio.sleep(0.3)

    async def _keepalive_loop(self) -> None:
        while self.connected:
            await asyncio.sleep(self.keepalive_interval)
            try:
                self.keepalive_sent += 1
                await self.write_setting(R.KEEP_ALIVE_SETTING, bytes([R.KEEP_ALIVE_VALUE]))
                self.keepalive_acked += 1
            except Timeout:
                continue
            except Disconnected:
                return
