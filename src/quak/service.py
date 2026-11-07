"""CameraService: one long-lived camera connection with observable state, for the GUI.

The CLI opens a session per command; the GUI keeps one open, watches status pushes and
reconnects when the link drops. Both use the same Camera, flows and network code.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import sys
import time
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from quak import flows
from quak.camera import Camera
from quak.connect import Target, open_camera
from quak.core import registry as R
from quak.core.errors import NetworkError, QuakError, Rejected
from quak.core.registry import Status
from quak.net.http import CameraHttp, MediaFile
from quak.session import Trace

WATCHED = [
    Status.BATTERY_BARS,
    Status.BUSY,
    Status.ENCODING,
    Status.REMAINING_VIDEO,
    Status.PHOTOS,
    Status.VIDEOS,
    Status.AP_MODE,
    Status.BATTERY_PERCENT,
    Status.READY,
    Status.PRESET_GROUP,
    Status.PRESET,
    Status.SCHEDULE_PRESET,
    Status.SCHEDULE_ARMED,
    Status.TURBO,
    Status.CAMERA_CONTROL,
    Status.CONNECTED_SSID,
    Status.PREVIEW_STREAM,
]


@dataclass
class LogLine:
    t: float
    direction: str
    channel: str
    hex: str
    meaning: str
    category: str


@dataclass
class CameraState:
    link: str = "offline"  # offline | connecting | ble | error
    error: str = ""
    model: str = ""
    firmware: str = ""
    name: str = ""
    statuses: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    caps: dict = field(default_factory=dict)
    presets: list = field(default_factory=list)
    clock_offset: float | None = None
    tz_minutes: int | None = None
    encoding_since: float | None = None
    http: bool = False
    live: dict = field(default_factory=dict)
    busy_action: str = ""

    def s(self, sid: int, default=None):
        return self.statuses.get(sid, default)

    @property
    def connected(self) -> bool:
        return self.link == "ble"

    @property
    def recording(self) -> bool:
        return bool(self.s(Status.ENCODING))

    @property
    def schedule(self) -> dict:
        value = self.settings.get(R.SCHEDULED_CAPTURE, 0)
        d = R.decode_schedule(value)
        return {
            "armed": bool(self.s(Status.SCHEDULE_ARMED)),
            "time": f"{d['hour']:02d}:{d['minute']:02d}",
            "preset": self.s(Status.SCHEDULE_PRESET),
            "duration": self.settings.get(R.VIDEO_DURATION),
        }


class CameraService:
    def __init__(self, target: Target | None = None, *, auto_reconnect: bool = True) -> None:
        self.target = target or Target()
        self.state = CameraState()
        self.log: deque[LogLine] = deque(maxlen=2000)
        self.auto_reconnect = auto_reconnect
        self._listeners: list[Callable[[CameraState], None]] = []
        self._log_listeners: list[Callable[[LogLine], None]] = []
        self._stack: contextlib.AsyncExitStack | None = None
        self._http_stack: contextlib.AsyncExitStack | None = None
        self._http: CameraHttp | None = None
        self.cam: Camera | None = None
        self._tasks: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()
        self._http_lock = asyncio.Lock()  # joining and leaving the camera's Wi-Fi: one at a time
        self._preview_port: int | None = None
        self.allow_busy_adapter = False  # the user agreed to take this computer's own Wi-Fi for the camera
        self._closing = False

    # ---------------------------------------------------------------- observers

    def subscribe(self, fn: Callable[[CameraState], None]) -> Callable[[], None]:
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None

    def subscribe_log(self, fn: Callable[[LogLine], None]) -> Callable[[], None]:
        self._log_listeners.append(fn)
        return lambda: self._log_listeners.remove(fn) if fn in self._log_listeners else None

    def _emit(self, **changes) -> None:
        self.state = replace(self.state, **changes) if changes else self.state
        for fn in list(self._listeners):
            with contextlib.suppress(Exception):
                fn(self.state)

    def clear_live_status(self) -> None:
        self._emit(live={})

    def _trace(self, t: Trace) -> None:
        line = LogLine(t.t, t.direction, R.short_uuid(t.uuid), t.text, t.meaning, t.category)
        self.log.append(line)
        for fn in list(self._log_listeners):
            with contextlib.suppress(Exception):
                fn(line)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ---------------------------------------------------------------- connection

    async def connect(self) -> None:
        if self.state.connected:
            return
        self._closing = False
        self._emit(link="connecting", error="")
        stack = contextlib.AsyncExitStack()
        try:
            target = replace(self.target, trace=self._trace)
            cam = await stack.enter_async_context(open_camera(target))
            self._stack, self.cam = stack, cam
            info = await cam.hardware_info()
            self._emit(model=info.model_name, firmware=info.firmware, name=info.ap_ssid)
            current, updates = await cam.watch_statuses(WATCHED)
            self._apply_statuses(current)
            if current.get(Status.TURBO):  # a connection dropped during a download: it blocks nearly everything else
                with contextlib.suppress(QuakError):
                    await cam.turbo(False)
            await self.refresh()
            self._emit(link="ble")
            self._spawn(self._pump(updates))
            self._spawn(self._watch_disconnect())
        except Exception as e:
            await stack.aclose()
            self._stack = self.cam = None
            self._emit(link="error", error=str(e))
            raise

    async def disconnect(self) -> None:
        self._closing = True
        await self.release_http()
        for t in list(self._tasks):
            t.cancel()
        if self._stack:
            with contextlib.suppress(Exception):
                await self._stack.aclose()
        self._stack = self.cam = None
        self._emit(link="offline")

    async def _watch_disconnect(self) -> None:
        await self.cam.s.disconnected.wait()
        if self._closing:
            return
        self._stack = self.cam = None
        self._emit(link="offline", error="the camera disconnected")
        if self.auto_reconnect:
            for delay in (2, 5, 10, 20, 30):
                await asyncio.sleep(delay)
                if self._closing:
                    return
                try:
                    await self.connect()
                    return
                except Exception:
                    continue

    async def _pump(self, updates: asyncio.Queue) -> None:
        while True:
            change = await updates.get()
            self._apply_statuses(change)
            if Status.PRESET in change or Status.SCHEDULE_ARMED in change:
                with contextlib.suppress(QuakError):
                    await self.refresh_settings()
            self._emit()

    def _apply_statuses(self, change: dict) -> None:
        if (
            self._preview_port is not None
            and change.get(Status.ENCODING) == 0
            and self.state.statuses.get(Status.ENCODING)
        ):
            self._spawn(self._preview_restart())
        statuses = {**self.state.statuses, **change}
        since = self.state.encoding_since
        if change.get(Status.ENCODING) == 1 and not since:
            since = time.time()
        elif change.get(Status.ENCODING) == 0:
            since = None
        self.state = replace(self.state, statuses=statuses, encoding_since=since)

    async def refresh(self) -> None:
        cam = self._need()
        statuses = await cam.statuses()
        self._apply_statuses(statuses)
        t, tz, _ = await cam.clock()
        self._emit(clock_offset=(t - dt.datetime.now()).total_seconds(), tz_minutes=tz)
        await self.refresh_settings()
        with contextlib.suppress(QuakError):
            self._emit(presets=await cam.presets())

    async def refresh_settings(self) -> None:
        cam = self._need()
        settings = await cam.settings()
        caps = await cam.capabilities()
        self._emit(settings=settings, caps=caps)

    def _need(self) -> Camera:
        if not self.cam:
            raise QuakError("not connected to a camera")
        return self.cam

    async def _do(self, label: str, coro_fn):
        async with self._lock:
            self._emit(busy_action=label, error="")
            try:
                return await coro_fn(self._need())
            except QuakError as e:
                self._emit(error=str(e))
                raise
            finally:
                self._emit(busy_action="")

    # ---------------------------------------------------------------- operations (same as the CLI)

    async def shutter(self, on: bool) -> None:
        await self._do("shutter", lambda c: c.shutter(on))

    async def hilight(self) -> None:
        await self._do("hilight", lambda c: c.hilight())

    async def sleep_camera(self) -> None:
        self._closing = True
        await self._do("sleep", lambda c: c.sleep())

    async def load_group(self, group: str) -> None:
        await self._do("preset", lambda c: c.load_group(group))
        await asyncio.sleep(0.3)
        await self.refresh_settings()

    async def load_preset(self, preset_id: int) -> None:
        await self._do("preset", lambda c: c.load_preset(preset_id))
        await asyncio.sleep(0.3)
        await self.refresh_settings()

    async def set_setting(self, sid: int, value: int) -> None:
        await self._do("setting", lambda c: c.set_setting(sid, value))
        await self.refresh_settings()

    async def sync_clock(self) -> None:
        await self._do("clock", lambda c: c.sync_clock())
        t, tz, _ = await self._need().clock()
        self._emit(clock_offset=(t - dt.datetime.now()).total_seconds(), tz_minutes=tz)

    async def arm_schedule(
        self, when: dt.datetime, duration: str | None, *, preset="video", sleep_after=False, allow_no_limit=False
    ) -> flows.ScheduleResult:
        if sleep_after:
            self._closing = True
        r = await self._do(
            "schedule",
            lambda c: flows.arm_schedule(
                c, when, duration=duration, preset=preset, sleep_after=sleep_after, allow_no_limit=allow_no_limit
            ),
        )
        if not sleep_after:
            await self.refresh_settings()
        return r

    async def disarm_schedule(self) -> None:
        await self._do("schedule", flows.disarm_schedule)
        await self.refresh_settings()

    async def set_ap(self, on: bool) -> None:
        await self._do("wifi", lambda c: c.set_ap(on))

    async def turbo(self, on: bool) -> None:
        await self._do("turbo", lambda c: c.turbo(on))

    async def scan_networks(self) -> list[dict]:
        return await self._do("wifi scan", lambda c: c.scan_networks())

    async def livestream(
        self,
        url: str,
        seconds: float,
        *,
        network=None,
        resolution=720,
        encode=False,
        on_log: Callable[[str], None] = lambda s: None,
    ) -> flows.LiveResult:
        def log(msg: str) -> None:
            on_log(msg)
            if msg.startswith("livestream: "):
                self._emit(live={**self.state.live, "state": msg.removeprefix("livestream: ")})

        def run():
            return self._do(
                "livestream",
                lambda c: flows.livestream(
                    c, url, seconds, resolution=resolution, encode=encode, network=network, log=log
                ),
            )

        try:
            return await run()
        except flows.LivestreamStuck:
            print(
                "quak: livestream stuck; resetting the camera (sleep and wake), then retrying",
                file=sys.stderr,
                flush=True,
            )
            log("livestream: the camera ignored the request; resetting it (sleep and wake), then trying again")
            await self.reset_camera()
            return await run()

    async def reset_camera(self) -> None:
        """Sleep the camera and wake it by reconnecting: clears a HERO11 that ignores livestream requests."""
        cam = self._need()
        self._closing = True  # we reconnect ourselves
        with contextlib.suppress(QuakError):
            await cam.sleep()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(cam.s.disconnected.wait(), 10)
        await self.disconnect()
        await asyncio.sleep(3)
        await self.connect()

    # ---------------------------------------------------------------- camera Wi-Fi / HTTP

    async def http(self) -> CameraHttp:
        # The GUI asks from several tasks at once (listing, thumbnails, delete). Two joins of the same
        # adapter knock each other off the network, so the first does the work and the rest wait for it.
        async with self._http_lock:
            if self._http:
                return self._http
            cam = self._need()
            stack = contextlib.AsyncExitStack()
            if self.target.sim:
                from quak.transport.sim_http import SimHttpServer

                server = SimHttpServer(cam.link.camera, asyncio.get_running_loop())
                api = CameraHttp(server.start())
                stack.callback(server.stop)
            else:
                from quak.net.wifi import camera_wifi

                api = await stack.enter_async_context(camera_wifi(cam, allow_busy_adapter=self.allow_busy_adapter))
            self._http_stack, self._http = stack, api
            self._emit(http=True)
            return api

    async def release_http(self) -> None:
        async with self._http_lock:
            if self._http_stack:
                with contextlib.suppress(Exception):
                    await self._http_stack.aclose()
            self._http_stack = self._http = None
            self._preview_port = None
            self._emit(http=False)

    async def media_list(self) -> list[MediaFile]:
        api = await self.http()
        return await asyncio.to_thread(api.media_list)

    async def thumbnail(self, f: MediaFile) -> bytes:
        api = await self.http()
        return await asyncio.to_thread(api.thumbnail, f.path)

    async def download(self, f: MediaFile, dest_dir: Path, progress=None) -> tuple[int, str, Path]:
        api = await self.http()
        with contextlib.suppress(QuakError):
            await self.turbo(True)
        try:
            return await asyncio.to_thread(api.download, f, dest_dir / f.name, progress)
        finally:
            with contextlib.suppress(QuakError):
                await self.turbo(False)

    async def delete(self, f: MediaFile) -> None:
        api = await self.http()
        try:
            await asyncio.to_thread(api.delete, f.path)
        except NetworkError:  # the camera-Wi-Fi link died (e.g. around a livestream): join again once
            await self.release_http()
            api = await self.http()
            await asyncio.to_thread(api.delete, f.path)

    async def preview_start(self, port: int = 8554) -> str:
        api = await self.http()
        await self._start_stream(api, port)
        self._preview_port = port
        self._spawn(self._preview_keepalive(api))
        return f"udp://@:{port}"

    async def _start_stream(self, api: CameraHttp, port: int) -> None:
        """A 409 usually means the camera believes a stream is already running, for example after an app was
        killed during preview (status 32 stays 1). Only a stop clears that; it can also just be busy for a moment."""
        for attempt in range(6):
            try:
                await asyncio.to_thread(api.preview_start, None if port == 8554 else port)
                return
            except Rejected:
                if attempt == 5:
                    raise
                with contextlib.suppress(QuakError):
                    await asyncio.to_thread(api.preview_stop)
                await asyncio.sleep(1.0)

    async def _preview_restart(self) -> None:
        """A HERO11 can't stream while recording, and doesn't bring the stream back afterwards by itself
        (measured: 530 KB/s, then nothing during and after the recording, until it's started again)."""
        await asyncio.sleep(1.0)
        api, port = self._http, self._preview_port
        if api is None or port is None:
            return
        with contextlib.suppress(QuakError):
            await asyncio.to_thread(api.preview_stop)
            await self._start_stream(api, port)

    async def _preview_keepalive(self, api: CameraHttp) -> None:
        while self._http is api:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(api.keep_alive)
            await asyncio.sleep(2.5)

    async def preview_stop(self) -> None:
        self._preview_port = None
        if self._http:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._http.preview_stop)
