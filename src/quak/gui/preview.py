"""Turn the camera's UDP MPEG-TS preview into JPEG frames (ffmpeg), for platforms without a
native video player in the app (web mode, `flet run`). Built apps use flet-video instead."""

from __future__ import annotations

import asyncio
import shutil
from typing import Callable

MAX_TRIES = 5  # ffmpeg starts in a row without a single frame, before giving up
RETRY_SECONDS = 0.5


class FrameDecoder:
    def __init__(
        self,
        port: int,
        on_frame: Callable[[bytes], None],
        fps: int = 10,
        width: int = 640,
        on_failed: Callable[[], None] | None = None,
    ) -> None:
        self.port, self.on_frame, self.fps, self.width = port, on_frame, fps, width
        self.on_failed = on_failed
        self.proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._tries = 0
        self.frames = 0

    @staticmethod
    def available() -> bool:
        return bool(shutil.which("ffmpeg"))

    async def start(self) -> None:
        await self._launch()
        self._task = asyncio.create_task(self._keep_running())

    async def _keep_running(self) -> None:
        """ffmpeg exits if it joins the stream at a bad moment and finds nothing to decode, which happened on about 1
        start in 10 on a real camera. The stream is still coming, so starting ffmpeg again is enough."""
        while True:
            await self._read()
            await self.proc.wait()
            self._tries += 1
            if self._tries >= MAX_TRIES:
                if self.on_failed:
                    self.on_failed()
                return
            await asyncio.sleep(RETRY_SECONDS)
            await self._launch()

    async def _launch(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-loglevel",
            "error",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-probesize",
            "1000000",
            "-analyzeduration",
            "2000000",
            "-i",
            f"udp://0.0.0.0:{self.port}?overrun_nonfatal=1&fifo_size=500000",
            "-vf",
            f"fps={self.fps},scale={self.width}:-2",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "-q:v",
            "6",
            "-",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def _read(self) -> None:
        buf = b""
        while self.proc and self.proc.stdout:
            chunk = await self.proc.stdout.read(65536)
            if not chunk:
                return
            buf += chunk
            while True:
                start = buf.find(b"\xff\xd8")
                end = buf.find(b"\xff\xd9", start + 2)
                if start < 0 or end < 0:
                    break
                frame, buf = buf[start : end + 2], buf[end + 2 :]
                self.frames += 1
                self._tries = 0
                self.on_frame(frame)
            if len(buf) > 4_000_000:
                buf = b""

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except asyncio.TimeoutError:
                self.proc.kill()
        self.proc = None
