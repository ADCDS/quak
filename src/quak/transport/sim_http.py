"""HTTP API of the simulated camera: media list/info/download/delete, thumbnails, preview, turbo.

Clips and photos are real files made with ffmpeg (test pattern) so downloads can be checked
with ffprobe; without ffmpeg they are placeholder bytes. The preview stream is a real
MPEG-TS test pattern sent over UDP to the client that asked for it, like the camera does.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from quak.core.registry import Status
from quak.transport.sim import SimCamera, SimMedia

FFMPEG = shutil.which("ffmpeg")
# a valid 8x8 grey JPEG, used when ffmpeg isn't available (phones)
TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/"
    "wAALCAAIAAgBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAACP/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AVN//2Q=="
)


def _picture_input(name: str = "") -> list[str]:
    """What the simulated camera sees: a test pattern, or a picture for screenshots (QUAK_SIM_PICTURE, see
    tools/make_images.py). Each clip gets its own framing of the picture, and the live view drifts slowly."""
    picture = os.environ.get("QUAK_SIM_PICTURE")
    if not picture:
        return ["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30"]
    n = sum(map(ord, name))
    x = (n * 67) % 380 if name else "190+150*sin(t/5)"
    y = (n * 41) % 214 if name else 107
    return ["-loop", "1", "-framerate", "30", "-i", picture, "-vf", f"crop=900:506:{x}:{y},scale=640:360"]


class SimHttpServer:
    def __init__(
        self,
        camera: SimCamera,
        loop: asyncio.AbstractEventLoop,
        host: str = "127.0.0.1",
        port: int = 0,
        media_dir: Path | None = None,
    ) -> None:
        self.camera, self.loop = camera, loop
        self._tmp = None if media_dir else tempfile.TemporaryDirectory(prefix="quak-sim-media-")
        self.media_dir = media_dir or Path(self._tmp.name)
        self.preview: subprocess.Popen | None = None
        self.camera.on_encoding_start = self._stop_preview
        self.stream_on = False
        self.preview_target: tuple[str, int] | None = None
        self.requests: list[str] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # quiet
                pass

            def do_GET(self):
                server.requests.append(self.path)
                try:
                    server.route(self)
                except FileNotFoundError:
                    self._send(404, b'{"error":"not found"}')
                except Exception as e:  # pragma: no cover - simulator bug
                    self._send(500, json.dumps({"error": str(e)}).encode())

            def _send(self, code: int, body: bytes, ctype: str = "application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.Handler = Handler
        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> str:
        self.thread.start()
        return self.url

    def stop(self) -> None:
        self._stop_preview()
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._tmp:
            self._tmp.cleanup()

    # ---------------------------------------------------------------- helpers

    def on_loop(self, fn):
        """Run fn() on the camera's event loop (camera state lives there) and wait for it."""

        async def call():
            return fn()

        return asyncio.run_coroutine_threadsafe(call(), self.loop).result(5)

    def _file_for(self, m: SimMedia) -> Path:
        path = self.media_dir / m.name
        if not path.exists():
            self._render(m, path)
        return path

    def _render(self, m: SimMedia, path: Path) -> None:
        if FFMPEG and m.name.endswith(".MP4"):
            subprocess.run(
                [
                    FFMPEG,
                    "-v",
                    "error",
                    "-y",
                    *_picture_input(m.name),
                    "-t",
                    f"{max(m.duration, 1.0):.2f}",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-pix_fmt",
                    "yuv420p",
                    str(path),
                ],
                check=True,
                timeout=60,
            )
        elif FFMPEG:
            subprocess.run(
                [FFMPEG, "-v", "error", "-y", *_picture_input(m.name), "-frames:v", "1", str(path)],
                check=True,
                timeout=30,
            )
        else:
            path.write_bytes(TINY_JPEG)

    def _thumb_for(self, m: SimMedia) -> bytes:
        thumb = self.media_dir / f"{m.name}.thumb.jpg"
        if not thumb.exists():
            if FFMPEG:
                subprocess.run(
                    [
                        FFMPEG,
                        "-v",
                        "error",
                        "-y",
                        "-i",
                        str(self._file_for(m)),
                        "-frames:v",
                        "1",
                        "-vf",
                        "scale=160:-1",
                        str(thumb),
                    ],
                    check=True,
                    timeout=30,
                )
            else:
                thumb.write_bytes(TINY_JPEG)
        return thumb.read_bytes()

    def _find(self, path: str) -> SimMedia:
        folder, _, name = path.strip("/").partition("/")
        for m in self.camera.media:
            if m.folder == folder and m.name == name:
                return m
        raise FileNotFoundError(path)

    def _stop_preview(self) -> None:
        if self.preview and self.preview.poll() is None:
            self.preview.terminate()
            try:
                self.preview.wait(5)
            except subprocess.TimeoutExpired:
                self.preview.kill()
        self.preview = None

    # ---------------------------------------------------------------- routes

    def route(self, h) -> None:
        u = urllib.parse.urlsplit(h.path)
        if "%2f" in u.query.lower():  # like the real camera (seen on a HERO11): an encoded slash in path= is a 400
            return h._send(400, b"{}\n")
        q = dict(urllib.parse.parse_qsl(u.query))
        cam = self.camera
        ok = lambda body=b"{}": h._send(200, body)  # noqa: E731
        if u.path == "/gopro/version":
            return ok(b'{"version": "2.0"}')
        if u.path == "/gopro/camera/state":
            state = self.on_loop(
                lambda: {
                    "status": {str(k): v for k, v in cam.status.items()},
                    "settings": {
                        str(k): cam.setting(k)
                        for k in sorted(set(cam.global_settings) | set(cam.preset_settings[cam.preset]))
                    },
                }
            )
            return ok(json.dumps(state).encode())
        if u.path == "/gopro/camera/keep_alive":
            return ok()
        if u.path == "/gopro/media/list":
            files = []
            for m in list(cam.media):
                f = self._file_for(m)
                files.append(
                    {
                        "n": m.name,
                        "cre": str(m.created),
                        "mod": str(m.created + int(m.duration)),
                        "s": str(f.stat().st_size),
                    }
                )
            return ok(json.dumps({"id": "1", "media": [{"d": "100GOPRO", "fs": files}] if files else []}).encode())
        if u.path == "/gopro/media/info":
            m = self._find(q["path"])
            return ok(
                json.dumps(
                    {
                        "cre": str(m.created),
                        "s": str(self._file_for(m).stat().st_size),
                        "dur": str(round(m.duration)),
                        "hc": str(len(m.hilights)),
                        "hi": m.hilights,
                        "w": "640",
                        "h": "360",
                    }
                ).encode()
            )
        if u.path == "/gopro/media/thumbnail":
            return h._send(200, self._thumb_for(self._find(q["path"])), "image/jpeg")
        if u.path.startswith("/videos/DCIM/"):
            m = self._find(urllib.parse.unquote(u.path[len("/videos/DCIM/") :]))
            return h._send(
                200, self._file_for(m).read_bytes(), "video/mp4" if m.name.endswith(".MP4") else "image/jpeg"
            )
        if u.path == "/gopro/media/delete/file":
            m = self._find(q["path"])

            def remove():
                cam.media.remove(m)
                sid = Status.VIDEOS if m.name.endswith(".MP4") else Status.PHOTOS
                cam._set_status(sid, int(cam.status[sid]) - 1)

            self.on_loop(remove)
            (self.media_dir / m.name).unlink(missing_ok=True)
            return ok()
        if u.path == "/gopro/media/turbo_transfer":
            self.on_loop(lambda: cam._set_status(Status.TURBO, int(q.get("p", "0"))))
            return ok()
        if u.path == "/gopro/camera/stream/start":
            if self.stream_on:  # like the real camera, even if the client that started it is gone
                return h._send(409, b"{}\n")
            self.stream_on = True
            port = int(q.get("port", 8554))
            self.preview_target = (h.client_address[0], port)
            if FFMPEG:
                self.preview = subprocess.Popen(
                    [
                        FFMPEG,
                        "-v",
                        "error",
                        "-re",
                        *_picture_input(),
                        "-c:v",
                        "libx264",
                        "-preset",
                        "ultrafast",
                        "-tune",
                        "zerolatency",
                        "-g",
                        "30",
                        "-f",
                        "mpegts",
                        f"udp://{h.client_address[0]}:{port}?pkt_size=1316",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            self.on_loop(lambda: cam._set_status(Status.PREVIEW_STREAM, 1))
            return ok()
        if u.path == "/gopro/camera/stream/stop":
            self.stream_on = False
            self._stop_preview()
            self.on_loop(lambda: cam._set_status(Status.PREVIEW_STREAM, 0))
            return ok()
        h._send(404, b'{"error":"unknown endpoint"}')
