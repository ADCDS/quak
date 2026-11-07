"""A simulated HERO11 Black that speaks the real BLE byte protocol.

It reassembles our framed writes, answers with framed notifications (20-byte packets,
configurable latency) and keeps enough state to exercise every Quak feature without the
camera: per-preset settings, Easy/Pro controls, allowed values, statuses with push
updates, clock with timezone, shutter and clips, HiLights, scheduled capture firing on
the camera clock, sleep, the camera's Wi-Fi AP, network provisioning and livestream.

Behaviour observed on a real HERO11 (fw 2.20) is reproduced as seen. Behaviour nobody has
verified yet is marked ASSUMED and listed in ASSUMPTIONS, so the hardware E2E run can
confirm or correct it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from quak.core import codec, proto
from quak.core import registry as R
from quak.core.errors import Disconnected, NotBonded
from quak.core.registry import Action, Cmd, Feature, Query, Status

ASSUMPTIONS = {  # "VERIFIED" = checked on a HERO11 Black, firmware 2.20 (Quak E2E, Nov 2026)
    "status_push_format": "VERIFIED: pushes are [0x93][0x00][TLV...], like a query reply",
    "local_datetime_reply": "VERIFIED: 0x10 replies with date/time, tz minutes (int16), dst",
    "schedule_one_shot": "VERIFIED: after firing, 168 is switched off and status 108 drops to 0",
    "preview_stops_when_recording": "VERIFIED: a recording ends the preview stream, and only stream/start brings it back",
    "stream_start_when_running": "VERIFIED: stream/start answers 409 while a stream is running, until stream/stop",
    "schedule_survives_sleep": "VERIFIED: a schedule armed before BLE sleep (0x05) still fires",
    "power_button_cancels_schedule": "VERIFIED False: switched off with the power button, the camera wakes and records",
    "duration_needs_video_or_timelapse": "156 has no allowed values while a photo preset is active",
    "sleep_disconnects": "the camera drops the BLE link right after answering 0x05",
    "preset_ids": "VERIFIED: the camera's own list (video 0x0009xxxx, photo 0x0001xxxx, time lapse 0x0002xxxx)",
    "release_network": "F1 78 is answered with RESULT_SUCCESS",
    "livestream_needs_network": "set-livestream-mode without a joined network ends in FAILED/NETWORK",
    "setting_sizes": "settings 30 and 32 travel as 4 bytes, like 168",
    "http_path_slash": "VERIFIED: path=100GOPRO/X.MP4 needs a literal slash; %2F is HTTP 400",
    "turbo_blocks_capture": "VERIFIED: in Turbo Transfer, setting writes answer 1 and starting the shutter answers 2",
}

# (id, title_id, flat mode, name)
PRESETS = {
    1000: [
        (0x00090000, 1, 12, "Standard"),
        (0x00090001, 0, 12, "Activity"),
        (0x00090002, 2, 12, "Cinematic"),
        (0x00090003, 11, 12, "Slo-Mo"),
    ],
    1001: [(0x00010000, 3, 16, "Photo"), (0x00010002, 5, 19, "Burst"), (0x00010003, 6, 18, "Night")],
    1002: [
        (0x00020000, 7, 24, "TimeWarp"),
        (0x00020003, 76, 29, "Star Trail"),
        (0x00020004, 77, 30, "Light Painting"),
        (0x00020005, 78, 31, "Light Trail"),
        (0x00020001, 8, 13, "Time Lapse"),
        (0x00020002, 9, 26, "Night Lapse"),
    ],
}
PRESET_GROUP_OF = {p[0]: g for g, ps in PRESETS.items() for p in ps}
PER_PRESET = {2, 3, 5, 30, 32, 121, 122, 123, 125, 135, 150, 151, 156, 157, 167, 176, 177, 179}
VIDEO_DEFAULTS = {2: 100, 3: 8, 121: 0, 135: 4, 156: 100, 167: 4}
GLOBAL_DEFAULTS = {59: 0, 83: 1, 91: 3, 168: 0, 175: 0, 178: 0, 180: 0}  # as read from the real camera

HW_INFO = [
    (58).to_bytes(4, "big"),
    b"HERO11 Black",
    b"0x05",
    b"H22.01.02.20.00",
    b"SIM0000000001",
    b"GP-SIM",
    b"0123456789ab",
]
HW_TRAILER = bytes.fromhex("0100010101 00025b5d0101")
WAP_SSID, WAP_PASSWORD = "GP-SIM", "sim-pass-1234"


def _first_option(sid: int) -> int:
    opts = R.SETTINGS[sid]["options"]
    return next(iter(opts)) if opts else 0


@dataclass
class SimNetwork:
    ssid: str
    password: str | None = None  # None = open network
    bars: int = 3
    mhz: int = 2437


@dataclass
class SimMedia:
    folder: str
    name: str
    created: int  # camera local time as epoch seconds, like the HTTP media list's "cre"
    size: int
    duration: float = 0.0
    hilights: list[int] = field(default_factory=list)  # ms offsets
    by_schedule: bool = False


class SimCamera:
    """One simulated camera. Thread-unsafe; use from one asyncio loop."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        latency: float = 0.002,
        packet_size: int = codec.PACKET_SIZE,
        bonded: bool = True,
        clock_offset: float = 4.0,
        tz_minutes: int = -180,
        dst: bool = False,
    ) -> None:
        self.clock = clock
        self.latency = latency
        self.packet_size = packet_size
        self.bonded = bonded
        self.pairing_mode = False
        self.clock_offset = clock_offset  # camera clock minus host local clock, seconds
        self.tz_minutes, self.dst = tz_minutes, dst
        self.schedule_survives_sleep = True
        self.power_button_cancels_schedule = False
        self.global_settings = {sid: _first_option(sid) for sid in R.SETTINGS if sid not in PER_PRESET}
        self.global_settings.update(GLOBAL_DEFAULTS)
        self.preset_settings: dict[int, dict[int, int]] = {}
        for group, presets in PRESETS.items():
            for pid, *_ in presets:
                vals = {sid: _first_option(sid) for sid in PER_PRESET if sid in R.SETTINGS}
                vals.update({156: 100, 157: 100})  # the real camera read No Limit
                if group == 1000:
                    vals.update(VIDEO_DEFAULTS)
                self.preset_settings[pid] = vals
        self.preset = 0x00010000  # the real camera sat on Photo
        self.status: dict[int, int | str] = {
            1: 1,
            2: 3,
            6: 0,
            8: 0,
            10: 0,
            13: 0,
            17: 1,
            19: 4,
            29: "",
            30: WAP_SSID,
            32: 0,
            33: 0,
            34: 1500,
            35: 6120,
            38: 38,
            39: 15,
            54: 48_000_000,
            69: 0,
            70: 78,
            82: 1,
            96: 1001,
            97: 0x00010000,
            107: 0xFFFFFFFF,
            108: 0,
            113: 0,
            114: 0,
        }
        self.media: list[SimMedia] = []
        self.counter = 64
        self.networks = [SimNetwork("Home Wi-Fi", "correct horse battery"), SimNetwork("Cafe guest", None, 1, 2462)]
        self.known_networks: set[str] = set()
        self.live = {"state": 0, "error": 0, "url": "", "encode": False, "window": 7, "registered": False, "bitrate": 0}
        self.on_stream: Callable[[str, bool], None] | None = None  # (url, started) hook for real RTMP output
        self.asleep = False
        self.log: list[tuple[str, str, bytes]] = []  # (direction, uuid, message) for tests
        # wiring
        self._subs: dict[str, Callable[[str, bytes], None]] = {}
        self._rx = {u: codec.Reassembler() for u in R.REQUEST_TO_RESPONSE}
        self._send_locks: dict[str, asyncio.Lock] = {}
        self._registered_statuses: set[int] = set()
        self._registered_settings: set[int] = set()
        self._preset_push = False
        self._tasks: set[asyncio.Task] = set()
        self._encoding: dict | None = None
        self.on_encoding_start = lambda: None  # the HTTP side ends its preview stream, as a real camera does
        self._scheduler: asyncio.Task | None = None
        self._last_fire_minute: str | None = None
        self.drop_replies = 0
        self.errors: list[str] = []
        self.keepalives = 0
        self.connected = False
        self._disconnect_cb: Callable[[], None] | None = None

    # -------------------------------------------------------------- derived state

    @property
    def group(self) -> int:
        return PRESET_GROUP_OF[self.preset]

    @property
    def pro(self) -> bool:
        return self.global_settings[175] == 1

    def setting(self, sid: int) -> int:
        if sid in PER_PRESET:
            return self.preset_settings[self.preset][sid]
        return self.global_settings[sid]

    def camera_now(self) -> dt.datetime:
        return dt.datetime.fromtimestamp(self.clock() + self.clock_offset).replace(microsecond=0)

    def allowed(self, sid: int) -> list[int]:
        if sid == R.SCHEDULED_CAPTURE:
            return [0] if self.pro else []
        if sid in R.PRO_ONLY and not self.pro:
            return []
        if sid == R.VIDEO_DURATION and self.group == 1001:  # ASSUMED duration_needs_video_or_timelapse
            return []
        if sid == R.MULTI_SHOT_DURATION and self.group != 1002:
            return []
        return list(R.SETTINGS[sid]["options"])

    # -------------------------------------------------------------- link side

    def attach(self, notify: Callable[[str, bytes], None], on_disconnect: Callable[[], None]) -> None:
        self._subs_all = notify
        self._disconnect_cb = on_disconnect
        self.connected = True
        if self.asleep:
            self.asleep = False
        self._ensure_scheduler()

    def subscribe(self, uuid: str) -> None:
        self._subs[uuid] = self._subs_all

    def detach(self) -> None:
        self.connected = False
        self._subs.clear()
        self._registered_statuses.clear()
        self._registered_settings.clear()
        self._preset_push = False
        self.live["registered"] = False
        if self.status[114] == 2:
            self._set_status(114, 0)

    def read_characteristic(self, uuid: str) -> bytes:
        if uuid == R.WAP_SSID:
            return WAP_SSID.encode()
        if uuid == R.WAP_PASSWORD:
            return WAP_PASSWORD.encode()
        if uuid == R.WAP_STATE:
            return bytes([self.status[69]])
        raise KeyError(uuid)

    def receive(self, uuid: str, packet: bytes) -> None:
        if uuid not in self._rx:
            raise KeyError(f"not writable: {uuid}")
        msg = self._rx[uuid].feed(packet)
        if msg is not None:
            self.log.append(("rx", uuid, msg))
            self._spawn(self._handle(uuid, msg))

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _emit(self, uuid: str, msg: bytes) -> None:
        lock = self._send_locks.setdefault(uuid, asyncio.Lock())
        async with lock:
            self.log.append(("tx", uuid, msg))
            for packet in codec.frame(msg, self.packet_size):
                await asyncio.sleep(self.latency)
                cb = self._subs.get(uuid)
                if cb and self.connected:
                    cb(uuid, packet)

    async def _reply(self, uuid: str, msg: bytes) -> None:
        if self.drop_replies > 0:
            self.drop_replies -= 1
            return
        await self._emit(R.REQUEST_TO_RESPONSE[uuid], msg)

    # -------------------------------------------------------------- dispatch

    async def _handle(self, uuid: str, msg: bytes) -> None:
        try:
            await self._dispatch(uuid, msg)
        except Exception as e:  # a simulator bug must not look like a silent camera
            self.errors.append(f"{type(e).__name__}: {e} while handling {msg.hex(' ')}")
            await self._reply(uuid, bytes([msg[0], 2]) if msg else b"")
            raise

    async def _dispatch(self, uuid: str, msg: bytes) -> None:
        if uuid == R.CQ_SETTINGS:
            await self._handle_setting(msg)
        elif uuid == R.CQ_QUERY and msg[0] == Feature.QUERY:
            await self._handle_proto_query(msg)
        elif uuid == R.CQ_QUERY:
            await self._handle_query(msg)
        elif uuid == R.CQ_COMMAND and msg[0] == Feature.COMMAND:
            await self._handle_proto_command(msg)
        elif uuid == R.CQ_COMMAND:
            await self._handle_command(msg)
        elif uuid == R.NET_MGMT:
            await self._handle_network(msg)

    async def _handle_setting(self, msg: bytes) -> None:
        sid, n = msg[0], msg[1]
        value = codec.to_int(msg[2 : 2 + n])
        if sid == R.KEEP_ALIVE_SETTING and value == R.KEEP_ALIVE_VALUE:  # 0x5B is also the LED setting (91)
            self.keepalives += 1
            return await self._reply(R.CQ_SETTINGS, bytes([sid, 0]))
        if sid not in R.SETTINGS:
            return await self._reply(R.CQ_SETTINGS, bytes([sid, 2]))
        allowed = self.allowed(sid)
        ok = (
            (value in allowed or (sid == R.SCHEDULED_CAPTURE and allowed))
            and not self._encoding
            and not self.status.get(Status.TURBO)
        )
        if not ok:
            return await self._reply(R.CQ_SETTINGS, bytes([sid, 1]))
        self._write_setting(sid, value)
        await self._reply(R.CQ_SETTINGS, bytes([sid, 0]))

    def _write_setting(self, sid: int, value: int) -> None:
        if sid in PER_PRESET:
            self.preset_settings[self.preset][sid] = value
        else:
            self.global_settings[sid] = value
        if sid == R.SCHEDULED_CAPTURE:
            armed = bool(value & 1)
            self._set_status(Status.SCHEDULE_ARMED, int(armed))
            self._set_status(Status.SCHEDULE_PRESET, self.preset if armed else 0xFFFFFFFF)
            self._last_fire_minute = None
        if sid in self._registered_settings:
            self._push(Query.SETTING_PUSH, {sid: codec.from_int(value, R.SETTING_SIZE.get(sid, 1))})

    def _set_status(self, sid: int, value: int | str) -> None:
        if self.status.get(sid) == value:
            return
        self.status[sid] = value
        if sid in self._registered_statuses:
            self._push(Query.STATUS_PUSH, {sid: self._status_bytes(sid)})  # ASSUMED status_push_format

    def _status_bytes(self, sid: int) -> bytes:
        v = self.status[sid]
        if isinstance(v, str):
            return v.encode()
        wide = sid in (
            Status.PRESET_GROUP,
            Status.PRESET,
            Status.SCHEDULE_PRESET,
            Status.REMAINING_KB,
            Status.REMAINING_VIDEO,
            Status.REMAINING_PHOTOS,
            Status.ENCODING_DURATION,
        )
        return codec.from_int(v, 4 if wide or v > 0xFF else 1)

    def _push(self, qid: int, values: dict[int, bytes]) -> None:
        body = b"".join(bytes([k, len(v)]) + v for k, v in values.items())
        self._spawn(self._emit(R.CQ_QUERY_RESP, bytes([qid, 0]) + body))

    async def _handle_query(self, msg: bytes) -> None:
        qid, ids = msg[0], list(msg[1:])
        if qid in (Query.STATUSES, Query.REGISTER_STATUSES):
            ids = ids or sorted(self.status)
            if qid == Query.REGISTER_STATUSES:
                self._registered_statuses.update(ids)
            body = b"".join(bytes([i, len(b)]) + b for i in ids if i in self.status for b in [self._status_bytes(i)])
            return await self._reply(R.CQ_QUERY, bytes([qid, 0]) + body)
        if qid in (Query.SETTINGS, Query.REGISTER_SETTINGS):
            ids = ids or sorted(R.SETTINGS)
            if qid == Query.REGISTER_SETTINGS:
                self._registered_settings.update(ids)
            body = b""
            for i in ids:
                if i in R.SETTINGS:
                    v = codec.from_int(self.setting(i), R.SETTING_SIZE.get(i, 1))
                    body += bytes([i, len(v)]) + v
            return await self._reply(R.CQ_QUERY, bytes([qid, 0]) + body)
        if qid == Query.CAPABILITIES:
            ids = ids or sorted(R.SETTINGS)
            body = b""
            for i in ids:
                if i not in R.SETTINGS:
                    continue
                allowed = self.allowed(i)
                if not allowed:
                    body += bytes([i, 0])
                for v in allowed:
                    b = codec.from_int(v, R.SETTING_SIZE.get(i, 1))
                    body += bytes([i, len(b)]) + b
            return await self._reply(R.CQ_QUERY, bytes([qid, 0]) + body)
        if qid in (Query.UNREGISTER_STATUSES, Query.UNREGISTER_SETTINGS):
            target = self._registered_statuses if qid == Query.UNREGISTER_STATUSES else self._registered_settings
            target.difference_update(ids or set(target))
            return await self._reply(R.CQ_QUERY, bytes([qid, 0]))
        await self._reply(R.CQ_QUERY, bytes([qid, 2]))

    async def _handle_command(self, msg: bytes) -> None:
        cmd = msg[0]
        params = [p for _, p in self._params(msg[1:])]
        reply = lambda status=0, extra=b"": self._reply(R.CQ_COMMAND, bytes([cmd, status]) + extra)  # noqa: E731
        if cmd == Cmd.SHUTTER:
            if self.status.get(Status.TURBO) and params[0] == b"\x01":
                return await reply(2)
            return await reply(self._shutter(params[0] == b"\x01"))
        if cmd == Cmd.SLEEP:
            await reply()
            self._spawn(self._go_to_sleep())
            return
        if cmd == Cmd.HILIGHT:
            if self._encoding:
                self._encoding["hilights"].append(int((self.clock() - self._encoding["start"]) * 1000))
            return await reply()
        if cmd in (Cmd.GET_DATETIME, Cmd.GET_LOCAL_DATETIME):
            t = self.camera_now()
            if cmd == Cmd.GET_DATETIME:
                value = codec.encode_datetime(t) + b"\x00"  # the real camera sends 8 bytes
            else:
                value = codec.encode_datetime(t, self.tz_minutes, self.dst)  # ASSUMED local_datetime_reply
            return await reply(0, bytes([len(value)]) + value)
        if cmd in (Cmd.SET_DATETIME, Cmd.SET_LOCAL_DATETIME):
            t, tz, dst = codec.decode_datetime(params[0])
            self.clock_offset = t.timestamp() - self.clock() + 0.5  # camera keeps whole seconds
            if tz is not None:
                self.tz_minutes, self.dst = tz, dst
            return await reply()
        if cmd == Cmd.AP_CONTROL:
            self._spawn(self._delayed_status(Status.AP_MODE, 1 if params[0] == b"\x01" else 0, 0.3))
            return await reply()
        if cmd == Cmd.HARDWARE_INFO:
            body = b"".join(bytes([len(f)]) + f for f in HW_INFO) + HW_TRAILER
            return await reply(0, body)
        if cmd == Cmd.LOAD_PRESET_GROUP:
            gid = codec.to_int(params[0])
            if gid not in PRESETS or self._encoding:
                return await reply(1)
            self._load_preset(PRESETS[gid][0][0] if self.group != gid else self.preset)
            return await reply()
        if cmd == Cmd.LOAD_PRESET:
            pid = codec.to_int(params[0])
            if pid not in PRESET_GROUP_OF or self._encoding:
                return await reply(1)
            self._load_preset(pid)
            return await reply()
        if cmd in (Cmd.CLIENT_INFO, Cmd.OPEN_GOPRO_VERSION):
            return await reply(0, b"\x01\x02\x01\x00" if cmd == Cmd.OPEN_GOPRO_VERSION else b"")
        await reply(2)

    @staticmethod
    def _params(data: bytes) -> list[tuple[int, bytes]]:
        out, i = [], 0
        while i < len(data):
            n = data[i]
            out.append((n, bytes(data[i + 1 : i + 1 + n])))
            i += 1 + n
        return out

    def _load_preset(self, pid: int) -> None:
        self.preset = pid
        self._set_status(Status.PRESET_GROUP, PRESET_GROUP_OF[pid])
        self._set_status(Status.PRESET, pid)
        if self._preset_push:
            self._spawn(
                self._emit(
                    R.CQ_QUERY_RESP,
                    bytes([Feature.QUERY, Action.PRESET_MODIFIED])
                    + proto.encode("NotifyPresetStatus", self._preset_status()),
                )
            )

    async def _delayed_status(self, sid: int, value: int, delay: float) -> None:
        await asyncio.sleep(delay)
        self._set_status(sid, value)

    # -------------------------------------------------------------- capture

    def _shutter(self, on: bool, by_schedule: bool = False) -> int:
        if self.live["state"] in (2, 3) and not by_schedule:  # shutter drives the livestream
            self._live_shutter(on)
            return 0
        if on:
            if self._encoding:
                return 1
            if self.group == 1001:  # photo: one picture
                self._add_media("photo", 0.0, [], by_schedule)
                return 0
            limit = None
            if self.group == 1000:
                limit = R.duration_seconds(self.setting(R.VIDEO_DURATION))
            self._encoding = {
                "start": self.clock(),
                "hilights": [],
                "by_schedule": by_schedule,
                "limit": limit,
                "created": self.camera_now(),
            }  # "cre" is the start of the recording
            self.on_encoding_start()
            self._set_status(Status.ENCODING, 1)
            self._set_status(Status.BUSY, 1)
            if limit:
                self._spawn(self._auto_stop(self._encoding, limit))
            return 0
        if not self._encoding:
            return 0
        self._stop_encoding()
        return 0

    async def _auto_stop(self, enc: dict, seconds: float) -> None:
        while self._encoding is enc and self.clock() - enc["start"] < seconds:
            await asyncio.sleep(0.05)
        if self._encoding is enc:
            self._stop_encoding()

    def _stop_encoding(self) -> None:
        enc, self._encoding = self._encoding, None
        duration = round(self.clock() - enc["start"], 2)
        self._add_media("video", duration, enc["hilights"], enc["by_schedule"], enc["created"])
        self._set_status(Status.ENCODING, 0)
        self._set_status(Status.BUSY, 0)

    def _add_media(
        self, kind: str, duration: float, hilights: list[int], by_schedule: bool, started: dt.datetime | None = None
    ) -> None:
        self.counter += 1
        created = int((started or self.camera_now()).replace(tzinfo=dt.timezone.utc).timestamp())  # local wall time
        if kind == "photo":
            m = SimMedia("100GOPRO", f"GOPR{self.counter:04d}.JPG", created, 4_200_000, 0.0, [], by_schedule)
            self._set_status(Status.PHOTOS, int(self.status[38]) + 1)
        else:
            m = SimMedia(
                "100GOPRO",
                f"GX01{self.counter:04d}.MP4",
                created,
                int(6_000_000 * max(duration, 1)),
                duration,
                list(hilights),
                by_schedule,
            )
            self._set_status(Status.VIDEOS, int(self.status[39]) + 1)
        self.media.append(m)

    # -------------------------------------------------------------- schedule

    def _ensure_scheduler(self) -> None:
        if self._scheduler is None or self._scheduler.done():
            try:
                self._scheduler = asyncio.get_running_loop().create_task(self._schedule_loop())
            except RuntimeError:
                self._scheduler = None

    async def _schedule_loop(self) -> None:
        while True:
            self.tick()
            await asyncio.sleep(0.05)

    def tick(self) -> None:
        value = self.global_settings[R.SCHEDULED_CAPTURE]
        if not value & 1:
            return
        s = R.decode_schedule(value)
        now = self.camera_now()
        key = now.strftime("%Y%m%d%H%M")
        if (now.hour, now.minute) == (s["hour"], s["minute"]) and key != self._last_fire_minute:
            self._last_fire_minute = key
            self.fire_schedule()

    def fire_schedule(self) -> None:
        pid = int(self.status[Status.SCHEDULE_PRESET])
        self.asleep = False
        if pid in PRESET_GROUP_OF:
            self._load_preset(pid)
        self._write_setting(R.SCHEDULED_CAPTURE, self.global_settings[R.SCHEDULED_CAPTURE] & ~1)  # ASSUMED one-shot
        self._shutter(True, by_schedule=True)

    def press_power_button(self) -> None:
        """What a person holding MODE until the camera turns off does."""
        if self.power_button_cancels_schedule:
            self._write_setting(R.SCHEDULED_CAPTURE, self.global_settings[R.SCHEDULED_CAPTURE] & ~1)
        self.asleep = True
        if self.connected and self._disconnect_cb:
            self._disconnect_cb()

    async def _go_to_sleep(self) -> None:
        await asyncio.sleep(0.2)
        if not self.schedule_survives_sleep:
            self._write_setting(R.SCHEDULED_CAPTURE, self.global_settings[R.SCHEDULED_CAPTURE] & ~1)
        self.asleep = True
        if self.connected and self._disconnect_cb:  # ASSUMED sleep_disconnects
            self._disconnect_cb()

    # -------------------------------------------------------------- protobuf

    async def _proto_reply(self, uuid: str, feature: int, action: int, message: str, values: dict) -> None:
        await self._reply(uuid, bytes([feature, R.response_action(action)]) + proto.encode(message, values))

    def _preset_status(self) -> dict:
        groups = []
        for gid, presets in PRESETS.items():
            groups.append(
                {
                    "id": gid,
                    "can_add_preset": True,
                    "icon": {1000: 0, 1001: 1, 1002: 2}[gid],
                    "mode_array": sorted({p[2] for p in presets}),
                    "preset_array": [
                        {
                            "id": pid,
                            "mode": mode,
                            "title_id": title,
                            "title_number": 0,
                            "user_defined": False,
                            "is_modified": False,
                            "is_fixed": True,
                            "is_visible": True,
                            "setting_array": [{"id": 2, "value": self.preset_settings[pid][2], "is_caption": True}],
                        }
                        for pid, title, mode, _ in presets
                    ],
                }
            )
        return {"preset_group_array": groups}

    async def _handle_proto_query(self, msg: bytes) -> None:
        action, body = msg[1], msg[2:]
        if action == Action.PRESET_STATUS:
            req = proto.decode("RequestGetPresetStatus", body)
            if req["register_preset_status"]:
                self._preset_push = True
            if req["unregister_preset_status"]:
                self._preset_push = False
            return await self._proto_reply(
                R.CQ_QUERY, Feature.QUERY, action, "NotifyPresetStatus", self._preset_status()
            )
        if action == Action.LIVESTREAM_STATUS:
            req = proto.decode("RequestGetLiveStreamStatus", body)
            if req["register_live_stream_status"]:
                self.live["registered"] = True
            if req["unregister_live_stream_status"]:
                self.live["registered"] = False
            return await self._proto_reply(
                R.CQ_QUERY, Feature.QUERY, action, "NotifyLiveStreamStatus", self._live_status()
            )
        await self._proto_reply(
            R.CQ_QUERY, Feature.QUERY, action, "ResponseGeneric", {"result": proto.RESULT_NOT_SUPPORTED}
        )

    async def _handle_proto_command(self, msg: bytes) -> None:
        action, body = msg[1], msg[2:]
        ok = {"result": proto.RESULT_SUCCESS}
        if action == Action.CAMERA_CONTROL:
            req = proto.decode("RequestSetCameraControlStatus", body)
            self._set_status(Status.CAMERA_CONTROL, req["camera_control_status"])
            return await self._proto_reply(R.CQ_COMMAND, Feature.COMMAND, action, "ResponseGeneric", ok)
        if action == Action.TURBO:
            req = proto.decode("RequestSetTurboActive", body)
            self._set_status(Status.TURBO, int(req["active"]))
            return await self._proto_reply(R.CQ_COMMAND, Feature.COMMAND, action, "ResponseGeneric", ok)
        if action == Action.LIVESTREAM_MODE:
            req = proto.decode("RequestSetLiveStreamMode", body)
            await self._proto_reply(R.CQ_COMMAND, Feature.COMMAND, action, "ResponseGeneric", ok)
            if req.get("url"):
                self.live.update(
                    url=req["url"], encode=req.get("encode", False), window=req.get("window_size", 7), error=0
                )
                self._spawn(self._live_configure())
            else:
                self._set_live(0)
            return
        if action == Action.RELEASE_NETWORK:  # ASSUMED release_network
            self._set_status(Status.CONNECTED_SSID, "")
            return await self._proto_reply(R.CQ_COMMAND, Feature.COMMAND, action, "ResponseGeneric", ok)
        await self._proto_reply(
            R.CQ_COMMAND, Feature.COMMAND, action, "ResponseGeneric", {"result": proto.RESULT_NOT_SUPPORTED}
        )

    # -------------------------------------------------------------- livestream

    def _live_status(self) -> dict:
        return {
            "live_stream_status": self.live["state"],
            "live_stream_error": self.live["error"],
            "live_stream_encode": self.live["encode"],
            "live_stream_bitrate": self.live["bitrate"],
            "live_stream_window_size_supported_array": [4, 7, 12],
            "live_stream_encode_supported": True,
            "live_stream_max_lens_unsupported": False,
            "live_stream_minimum_stream_bitrate": 800,
            "live_stream_maximum_stream_bitrate": 8000,
            "live_stream_lens_supported": True,
            "live_stream_lens_supported_array": [0, 3, 4],
        }

    def _set_live(self, state: int, error: int = 0) -> None:
        self.live.update(state=state, error=error)
        if self.live["registered"]:
            self._spawn(
                self._emit(
                    R.CQ_QUERY_RESP,
                    bytes([Feature.QUERY, Action.LIVESTREAM_NOTIF])
                    + proto.encode("NotifyLiveStreamStatus", self._live_status()),
                )
            )

    async def _live_configure(self) -> None:
        self._set_live(1)
        await asyncio.sleep(0.3)
        if not self.status[Status.CONNECTED_SSID]:  # ASSUMED livestream_needs_network
            self._set_live(5, 1)
        else:
            self._set_live(2)

    def _live_shutter(self, on: bool) -> None:
        if on and self.live["state"] == 2:
            self.live["bitrate"] = 4000
            self._set_live(3)
            if self.on_stream:
                self.on_stream(self.live["url"], True)
        elif not on and self.live["state"] == 3:
            if self.on_stream:
                self.on_stream(self.live["url"], False)
            self.live["bitrate"] = 0
            self._set_live(4)

    # -------------------------------------------------------------- network management

    async def _handle_network(self, msg: bytes) -> None:
        feature, action, body = msg[0], msg[1], msg[2:]
        nm = lambda a, m, v: self._proto_reply(R.NET_MGMT, feature, a, m, v)  # noqa: E731
        if feature == Feature.PAIRING and action == Action.PAIRING_FINISH:
            proto.decode("RequestPairingFinish", body)
            self.pairing_mode = False
            return await nm(action, "ResponseGeneric", {"result": proto.RESULT_SUCCESS})
        if action == Action.SCAN_NETWORKS:
            await nm(action, "ResponseStartScanning", {"result": 1, "scanning_state": 2})
            self._spawn(self._finish_scan())
            return
        if action == Action.GET_AP_ENTRIES:
            req = proto.decode("RequestGetApEntries", body)
            entries = [
                {
                    "ssid": n.ssid,
                    "signal_strength_bars": n.bars,
                    "signal_frequency_mhz": n.mhz,
                    "scan_entry_flags": self._flags(n),
                }
                for n in self.networks
            ]
            entries = entries[req.get("start_index", 0) : req.get("start_index", 0) + req.get("max_entries", 100)]
            return await nm(
                action, "ResponseGetApEntries", {"result": 1, "scan_id": req.get("scan_id", 1), "entries": entries}
            )
        if action in (Action.CONNECT, Action.CONNECT_NEW):
            req = proto.decode("RequestConnect" if action == Action.CONNECT else "RequestConnectNew", body)
            net = next((n for n in self.networks if n.ssid == req["ssid"]), None)
            if net is None:
                state = 7
            elif action == Action.CONNECT:
                state = 6 if net.ssid in self.known_networks else 8
            else:
                state = 5 if (net.password is None or req.get("password") == net.password) else 8
            message = "ResponseConnect" if action == Action.CONNECT else "ResponseConnectNew"
            await nm(action, message, {"result": 1, "provisioning_state": 2, "timeout_seconds": 30})
            self._spawn(self._finish_connect(net, state))
            return
        await nm(action, "ResponseGeneric", {"result": proto.RESULT_NOT_SUPPORTED})

    def _flags(self, n: SimNetwork) -> int:
        flags = 0 if n.password is None else proto.SCAN_FLAG_AUTHENTICATED
        if n.ssid in self.known_networks:
            flags |= proto.SCAN_FLAG_CONFIGURED
        if self.status[Status.CONNECTED_SSID] == n.ssid:
            flags |= proto.SCAN_FLAG_ASSOCIATED
        return flags

    async def _finish_scan(self) -> None:
        await asyncio.sleep(0.3)
        await self._emit(
            R.NET_MGMT_RESP,
            bytes([Feature.NETWORK, Action.NOTIF_SCAN])
            + proto.encode(
                "NotifStartScanning",
                {
                    "scanning_state": 5,
                    "scan_id": 1,
                    "total_entries": len(self.networks),
                    "total_configured_ssid": len(self.known_networks),
                },
            ),
        )

    async def _finish_connect(self, net: SimNetwork | None, state: int) -> None:
        await asyncio.sleep(0.3)
        if state in proto.PROVISIONING_DONE and net:
            self.known_networks.add(net.ssid)
            self._set_status(Status.CONNECTED_SSID, net.ssid)
            self._set_status(Status.AP_MODE, 0)
        await self._emit(
            R.NET_MGMT_RESP,
            bytes([Feature.NETWORK, Action.NOTIF_PROVISIONING])
            + proto.encode("NotifProvisioningState", {"provisioning_state": state}),
        )

    # -------------------------------------------------------------- persistence (for `quak --sim`)

    def to_json(self) -> dict:
        return {
            "saved_at": self.clock(),
            "bonded": self.bonded,
            "pairing_mode": self.pairing_mode,
            "clock_offset": self.clock_offset,
            "tz": self.tz_minutes,
            "dst": self.dst,
            "global": self.global_settings,
            "presets": {str(k): v for k, v in self.preset_settings.items()},
            "preset": self.preset,
            "status": self.status,
            "counter": self.counter,
            "media": [m.__dict__ for m in self.media],
            "known_networks": sorted(self.known_networks),
            "last_fire": self._last_fire_minute,
        }

    @classmethod
    def from_json(cls, data: dict, **kw) -> "SimCamera":
        cam = cls(**kw)
        cam.clock_offset, cam.tz_minutes, cam.dst = data["clock_offset"], data["tz"], data["dst"]
        cam.bonded, cam.pairing_mode = data.get("bonded", True), data.get("pairing_mode", False)
        cam.global_settings = {int(k): v for k, v in data["global"].items()}
        cam.preset_settings = {int(k): {int(s): v for s, v in vals.items()} for k, vals in data["presets"].items()}
        cam.preset = data["preset"]
        cam.status = {int(k): v for k, v in data["status"].items()}
        cam.status[Status.ENCODING] = cam.status[Status.BUSY] = 0
        cam.counter = data["counter"]
        cam.media = [SimMedia(**m) for m in data["media"]]
        cam.known_networks = set(data["known_networks"])
        cam._last_fire_minute = data.get("last_fire")
        cam._catch_up(data["saved_at"])
        return cam

    def _catch_up(self, since: float) -> None:
        """Between CLI runs nothing ticks; fire a schedule whose time passed meanwhile."""
        value = self.global_settings[R.SCHEDULED_CAPTURE]
        if not value & 1:
            return
        s = R.decode_schedule(value)
        start = dt.datetime.fromtimestamp(since + self.clock_offset)
        fire = start.replace(hour=s["hour"], minute=s["minute"], second=0, microsecond=0)
        if fire < start:
            fire += dt.timedelta(days=1)
        if fire <= self.camera_now():
            pid = int(self.status[Status.SCHEDULE_PRESET])
            if pid in PRESET_GROUP_OF:
                self.preset = pid
                self.status[Status.PRESET], self.status[Status.PRESET_GROUP] = pid, PRESET_GROUP_OF[pid]
            secs = R.duration_seconds(self.setting(R.VIDEO_DURATION)) if self.group == 1000 else 0
            self.counter += 1
            created = int(fire.replace(tzinfo=dt.timezone.utc).timestamp())
            if self.group == 1001:
                self.media.append(SimMedia("100GOPRO", f"GOPR{self.counter:04d}.JPG", created, 4_200_000, 0, [], True))
                self.status[Status.PHOTOS] = int(self.status[38]) + 1
            else:
                self.media.append(
                    SimMedia(
                        "100GOPRO",
                        f"GX01{self.counter:04d}.MP4",
                        created,
                        6_000_000 * (secs or 60),
                        float(secs or 60),
                        [],
                        True,
                    )
                )
                self.status[Status.VIDEOS] = int(self.status[39]) + 1
            self.global_settings[R.SCHEDULED_CAPTURE] = value & ~1
            self.status[Status.SCHEDULE_ARMED], self.status[Status.SCHEDULE_PRESET] = 0, 0xFFFFFFFF

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), indent=1))

    @classmethod
    def load_or_new(cls, path: Path, **kw) -> "SimCamera":
        if path.is_file():
            return cls.from_json(json.loads(path.read_text()), **kw)
        return cls(**kw)

    async def close(self) -> None:
        if self._scheduler:
            self._scheduler.cancel()
        for t in list(self._tasks):
            t.cancel()


class SimLink:
    """Link implementation backed by a SimCamera in the same process."""

    def __init__(self, camera: SimCamera, address: str = "SIM") -> None:
        self.camera = camera
        self.address = address
        self.on_disconnect: Callable[[], None] | None = None
        self._connected = False
        self._callbacks: dict[str, Callable[[str, bytes], None]] = {}

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> bool:
        new_bond = False
        if not self.camera.bonded:
            if not self.camera.pairing_mode:
                raise NotBonded("not bonded and the simulated camera is not in pairing mode")
            self.camera.bonded, new_bond = True, True
        self._connected = True
        self.camera.attach(self._notify, self._camera_dropped)
        return new_bond

    def _notify(self, uuid: str, packet: bytes) -> None:
        cb = self._callbacks.get(uuid)
        if cb:
            cb(uuid, packet)

    def _camera_dropped(self) -> None:
        if self._connected:
            self._connected = False
            self.camera.detach()
            if self.on_disconnect:
                self.on_disconnect()

    async def disconnect(self) -> None:
        if self._connected:
            self._connected = False
            self.camera.detach()

    async def write(self, uuid: str, data: bytes) -> None:
        if not self._connected:
            raise Disconnected("simulated link is down")
        await asyncio.sleep(0)
        self.camera.receive(uuid, data)

    async def read(self, uuid: str) -> bytes:
        if not self._connected:
            raise Disconnected("simulated link is down")
        return self.camera.read_characteristic(uuid)

    async def start_notify(self, uuid: str, callback: Callable[[str, bytes], None]) -> None:
        self._callbacks[uuid] = callback
        self.camera.subscribe(uuid)
