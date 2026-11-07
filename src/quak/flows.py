"""Multi-step operations shared by the CLI, the GUI and the E2E runner."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import time
from dataclasses import dataclass, field
from typing import Callable

from quak.camera import Camera
from quak.core import registry as R
from quak.core.errors import QuakError, Rejected
from quak.core.registry import Status


@dataclass
class ScheduleResult:
    at: dt.datetime
    preset: int
    duration: str
    clock_synced: bool
    slept: bool
    warnings: list[str] = field(default_factory=list)


async def arm_schedule(
    cam: Camera,
    when: dt.datetime,
    *,
    duration: str | None,
    preset: str | int = "video",
    sleep_after: bool = False,
    allow_no_limit: bool = False,
    sync_clock: bool = True,
    guard=None,
    log: Callable[[str], None] = lambda s: None,
) -> ScheduleResult:
    """Arm scheduled capture the way it actually works on a HERO11:
    Pro controls, synced clock, the right preset, a duration, then setting 168."""
    if duration is None and not allow_no_limit:
        raise ValueError(
            "a scheduled capture without a duration records until the card or battery runs out; "
            "pass a duration (e.g. 15s) or allow no limit explicitly"
        )
    set_setting = guard.set_setting if guard else cam.set_setting
    warnings: list[str] = []
    if await cam.setting(R.CONTROL_MODE) != 1:
        log("switching to Pro controls (scheduled capture is hidden in Easy)")
        await set_setting(R.CONTROL_MODE, 1)
        warnings.append("the camera stays on Pro controls")
        await asyncio.sleep(0.5)
    if sync_clock:
        await cam.sync_clock()
        log("camera clock synced")
    if isinstance(preset, str):
        log(f"loading the {preset} preset group")
        if guard:
            await guard.load_group(preset)
        else:
            await cam.load_group(preset)
        expected_group = preset
    else:
        await (guard.load_preset(preset) if guard else cam.load_preset(preset))
        expected_group = R.preset_group_of(preset)
    await asyncio.sleep(0.3)
    if duration is not None or allow_no_limit:
        value = R.duration_value(duration or "no limit")
        allowed = (await cam.capabilities([R.VIDEO_DURATION])).get(R.VIDEO_DURATION, [])
        if allowed and value not in allowed:
            raise Rejected("duration", f"{R.value_label(R.VIDEO_DURATION, value)} not allowed in this preset")
        if allowed:
            previous = await cam.setting(R.VIDEO_DURATION)
            await set_setting(R.VIDEO_DURATION, value)
            if value != previous:
                warnings.append(
                    f"Video Duration is now {R.value_label(R.VIDEO_DURATION, value)} (it was "
                    f"{R.value_label(R.VIDEO_DURATION, previous)}), which also limits recordings you start "
                    "yourself. Change it back in the camera's settings when you're done"
                )
        elif duration is not None:
            warnings.append("this preset has no Duration setting; the capture length is the preset's default")
    await set_setting(R.SCHEDULED_CAPTURE, R.encode_schedule(when.hour, when.minute))
    st = await cam.statuses([Status.SCHEDULE_ARMED, Status.SCHEDULE_PRESET, Status.PRESET])
    if st[Status.SCHEDULE_ARMED] != 1:
        raise Rejected("scheduled capture", "the camera accepted 168 but status 108 is not armed")
    pid = int(st[Status.SCHEDULE_PRESET])
    if R.preset_group_of(pid) != expected_group:
        raise Rejected(
            "scheduled capture",
            f"armed with preset 0x{pid:08x} ({R.preset_group_of(pid)}), not a {expected_group} preset",
        )
    if await cam.setting(R.AUTO_POWER_DOWN) == 0:
        warnings.append("Auto Power Off is Never: the camera stays on after the capture until its battery runs out")
    slept = False
    if sleep_after:
        await cam.sleep()
        slept = True
    label = R.value_label(R.VIDEO_DURATION, R.duration_value(duration)) if duration else "No Limit"
    return ScheduleResult(when, pid, label, sync_clock, slept, warnings)


async def disarm_schedule(cam: Camera) -> None:
    if await cam.setting(R.CONTROL_MODE) != 1:
        await cam.set_setting(R.CONTROL_MODE, 1)
    await cam.set_setting(R.SCHEDULED_CAPTURE, 0)


async def schedule_state(cam: Camera) -> dict:
    value = await cam.setting(R.SCHEDULED_CAPTURE)
    st = await cam.statuses([Status.SCHEDULE_ARMED, Status.SCHEDULE_PRESET])
    s = R.decode_schedule(value)
    return {
        "armed": bool(st[Status.SCHEDULE_ARMED]),
        "enabled": s["enabled"],
        "time": f"{s['hour']:02d}:{s['minute']:02d}",
        "preset": st[Status.SCHEDULE_PRESET],
        "value": value,
    }


@dataclass
class ClipResult:
    seconds: float
    videos_before: int
    videos_after: int
    started: float
    stopped: float
    hilights: int


async def record_clip(
    cam: Camera, seconds: float, *, hilight_at: float | None = None, group: str = "video", guard=None
) -> ClipResult:
    """Record a clip of about `seconds` in the given preset group and report the video count change."""
    if guard:
        await guard.load_group(group)
    else:
        await cam.load_group(group)
    before = int(await cam.status(Status.VIDEOS))
    started = time.time()
    await cam.shutter(True)
    tags = 0
    try:
        await cam.wait_status(Status.ENCODING, lambda v: v == 1, 5, poll=0.2)
        if hilight_at is not None:
            await asyncio.sleep(max(0.0, hilight_at - (time.time() - started)))
            await cam.hilight()
            tags = 1
        await asyncio.sleep(max(0.0, seconds - (time.time() - started)))
    finally:  # also when interrupted: never leave the camera recording
        with contextlib.suppress(QuakError):
            await cam.shutter(False)
    stopped = time.time()
    await cam.wait_status(Status.ENCODING, lambda v: v == 0, 15, poll=0.3)
    after = int(await cam.wait_status(Status.VIDEOS, lambda v: int(v) > before, 15, poll=0.3))
    return ClipResult(stopped - started, before, after, started, stopped, tags)


@dataclass
class LiveResult:
    states: list[str]
    bitrates: list[int]
    streamed_s: float
    network_state: str | None


async def join_camera_network(
    cam: Camera, ssid: str, password: str | None, log: Callable[[str], None] = lambda s: None
) -> str:
    """Make the camera join `ssid` (scan first, reuse a remembered entry if the camera has one)."""
    from quak.core import proto

    log("camera scanning for Wi-Fi networks")
    entries = await cam.scan_networks()
    entry = next((e for e in entries if e.get("ssid") == ssid), None)
    if entry is None:
        raise Rejected(f"join {ssid!r}", f"the camera can't see it (sees: {', '.join(e['ssid'] for e in entries)})")
    configured = bool(entry.get("scan_entry_flags", 0) & proto.SCAN_FLAG_CONFIGURED)
    log(f"camera joining {ssid} ({'remembered' if configured else 'new'} network)")
    state = await cam.join_network(ssid, password, configured)
    return proto.PROVISIONING[state]


WEBCAM_LENS = 43  # Webcam Digital Lenses: 0 Wide, 2 Narrow, 3 Superview, 4 Linear
LIVESTREAM_LENS = {0: "wide", 3: "superview", 4: "linear"}  # Narrow isn't a livestream lens


async def livestream(
    cam: Camera,
    url: str,
    seconds: float,
    *,
    resolution: int = 720,
    encode: bool = False,
    network: tuple[str, str | None] | None = None,
    ready_timeout: float = 60.0,
    lens: str | None = None,
    log: Callable[[str], None] = lambda s: None,
) -> LiveResult:
    """Stream to an RTMP url for `seconds`, in GoPro's documented order, and clean up."""
    from quak.core import proto
    from quak.core.errors import Timeout, Unsupported

    net_state = None
    q = cam.s.subscribe((R.CQ_QUERY_RESP, R.Feature.QUERY, R.Action.LIVESTREAM_NOTIF))
    states: list[str] = []
    bitrates: list[int] = []

    def note(status: dict) -> int:
        st = status.get("live_stream_status", 0)
        name = proto.LIVE_STREAM_STATE.get(st, str(st))
        if not states or states[-1] != name:
            states.append(name)
            log(
                f"livestream: {name}"
                + (
                    f" ({proto.LIVE_STREAM_ERROR.get(status.get('live_stream_error'))})"
                    if status.get("live_stream_error")
                    else ""
                )
            )
        if status.get("live_stream_bitrate"):
            bitrates.append(status["live_stream_bitrate"])
        if st == 5:
            raise Rejected("livestream", proto.LIVE_STREAM_ERROR.get(status.get("live_stream_error"), "failed"))
        return st

    async def wait_for(target: int, timeout: float) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            left = deadline - asyncio.get_running_loop().time()
            if left <= 0:
                raise Timeout(f"livestream never reached {proto.LIVE_STREAM_STATE[target]} (states: {states})")
            try:
                _, msg = await asyncio.wait_for(q.get(), min(left, 2.0))
                if note(proto.decode("NotifyLiveStreamStatus", msg[2:])) == target:
                    return
            except asyncio.TimeoutError:
                if note(await cam.livestream_status()) == target:
                    return

    started = 0.0
    original_preset = int(await cam.status(R.Status.PRESET))
    try:
        if network:  # inside the try: a failed join must still get the cleanup below, or the camera stays stranded
            try:
                net_state = await join_camera_network(cam, *network, log=log)
            except Timeout as e:
                # seen on a HERO11: it associates and finishes the Wi-Fi handshake, then never asks for an address
                raise LivestreamStuck(str(e)) from None
        note(await cam.livestream_status(register=True))
        try:
            await cam.shutter(False)
        except QuakError:
            pass
        # the request's lens is stored in setting 43 (Webcam Digital Lenses), which can't be written
        # any other way: send the one the camera has, so streaming doesn't change the user's setting
        lens = lens or LIVESTREAM_LENS.get(await cam.setting(WEBCAM_LENS), "wide")
        await cam.set_livestream_mode(url, encode=encode, resolution=resolution, lens=lens)
        try:
            await wait_for(2, ready_timeout)  # READY
        except Timeout:
            if states == ["IDLE"]:
                raise LivestreamStuck(
                    "the camera ignored the livestream request (stayed IDLE); a HERO11 does "
                    "this after its Wi-Fi was just switched off, and a sleep/wake resets it"
                )
            raise
        await asyncio.sleep(2)  # GoPro's SDK waits here too
        await cam.shutter(True)
        await wait_for(3, 30)  # STREAMING
        started = time.time()
        while time.time() - started < seconds:
            try:
                _, msg = await asyncio.wait_for(q.get(), 1.0)
                note(proto.decode("NotifyLiveStreamStatus", msg[2:]))
            except asyncio.TimeoutError:
                pass
    finally:
        streamed = time.time() - started if started else 0.0
        with contextlib.suppress(QuakError):
            await cam.shutter(False)
        with contextlib.suppress(QuakError):
            await cam.set_livestream_mode("")
        # until a preset is loaded again the camera stays in livestream mode and refuses preset loads
        for attempt in range(3):
            try:
                await cam.load_preset(original_preset)
                break
            except QuakError:
                await asyncio.sleep(1.5)
        else:
            with contextlib.suppress(QuakError):
                await cam.load_group("video")
        if network:
            with contextlib.suppress(Unsupported, QuakError):
                await cam.release_network()
        cam.s.unsubscribe(q)
    return LiveResult(states, bitrates, streamed, net_state)


class QuakFlowError(QuakError):
    pass


class LivestreamStuck(QuakFlowError):
    """SetLiveStreamMode accepted but the camera never left IDLE (seen on a HERO11 right after its AP went off)."""
