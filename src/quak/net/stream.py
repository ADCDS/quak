"""ffmpeg/ffprobe helpers: probe files and UDP streams, receive RTMP, push a test pattern."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from quak.core.errors import NetworkError

FFMPEG, FFPROBE = shutil.which("ffmpeg"), shutil.which("ffprobe")


def _need(tool: str | None, name: str) -> str:
    if not tool:
        raise NetworkError(f"{name} not found; install ffmpeg")
    return tool


def _summary(data: dict) -> dict:
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    out = {"video": None, "duration": float(data.get("format", {}).get("duration") or 0)}
    if video:
        frames = video.get("nb_read_frames") or video.get("nb_frames")
        out["video"] = {
            "codec": video.get("codec_name"),
            "width": video.get("width"),
            "height": video.get("height"),
            "frames": int(frames) if frames and str(frames).isdigit() else None,
        }
    return out


def probe_file(path: Path, count_frames: bool = False) -> dict:
    args = [
        _need(FFPROBE, "ffprobe"),
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,codec_name,width,height,nb_frames,nb_read_frames:format=duration",
        "-of",
        "json",
    ]
    if count_frames:
        args.append("-count_frames")
    p = subprocess.run(args + [str(path)], capture_output=True, text=True, timeout=120)
    if p.returncode:
        raise NetworkError(f"ffprobe {path}: {p.stderr.strip()}")
    return _summary(json.loads(p.stdout or "{}"))


def probe_udp(port: int, seconds: float = 15.0) -> dict:
    """Listen on UDP `port` for an MPEG-TS stream (the camera's preview) and describe it."""
    url = f"udp://0.0.0.0:{port}?timeout={int(seconds * 1_000_000)}&overrun_nonfatal=1&fifo_size=500000"
    args = [
        _need(FFPROBE, "ffprobe"),
        "-v",
        "error",
        "-analyzeduration",
        "3000000",
        "-probesize",
        "5000000",
        "-show_entries",
        "stream=codec_type,codec_name,width,height",
        "-of",
        "json",
        url,
    ]
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=seconds + 10)
    except subprocess.TimeoutExpired:
        raise NetworkError(f"no stream on UDP {port} within {seconds:.0f} s") from None
    if p.returncode:
        raise NetworkError(f"no stream on UDP {port}: {p.stderr.strip()[:200]}")
    return _summary(json.loads(p.stdout or "{}"))


class RtmpReceiver:
    """A one-shot RTMP server (ffmpeg -listen 1) that saves what it receives to a file."""

    def __init__(self, out: Path, port: int = 1935, app: str = "live", key: str = "quak", timeout: int = 120) -> None:
        self.out, self.port, self.path, self.timeout = out, port, f"{app}/{key}", timeout
        self.proc: subprocess.Popen | None = None

    def url_for(self, host: str) -> str:
        return f"rtmp://{host}:{self.port}/{self.path}"

    def start(self, host: str = "127.0.0.1") -> None:
        """Listen on one address only: whoever can reach this can push video to this computer."""
        self.out.parent.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [
                _need(FFMPEG, "ffmpeg"),
                "-v",
                "error",
                "-y",
                "-listen",
                "1",
                "-timeout",
                str(self.timeout),
                "-i",
                f"rtmp://{host}:{self.port}/{self.path}",
                "-c",
                "copy",
                "-f",
                "mpegts",
                str(self.out),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    def stop(self, grace: float = 8.0) -> str:
        if not self.proc:
            return ""
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            _, err = self.proc.communicate(timeout=grace)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            _, err = self.proc.communicate()
        return (err or b"").decode(errors="replace")


class TestPattern:
    """Push a test pattern to an RTMP URL (used by the simulated camera's livestream)."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None

    def start(self, url: str, width: int = 1280, height: int = 720) -> None:
        self.stop()
        self.proc = subprocess.Popen(
            [
                _need(FFMPEG, "ffmpeg"),
                "-v",
                "error",
                "-re",
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size={width}x{height}:rate=30",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-tune",
                "zerolatency",
                "-g",
                "30",
                "-f",
                "flv",
                url,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


class UdpHttpRelay:
    """The camera's preview (MPEG-TS over UDP) re-served as an endless HTTP stream on 127.0.0.1.

    For video players that can't open udp:// themselves (the phone's media_kit build never
    opened the port). Packets are counted, so "nothing arrives" and "the player won't play"
    can be told apart.
    """

    def __init__(self, udp_port: int = 8554) -> None:
        import socket
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.packets = 0
        self._clients: list = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.udp.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        self.udp.bind(("0.0.0.0", udp_port))
        self.udp.settimeout(0.5)
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                import queue

                q: queue.Queue = queue.Queue(maxsize=2000)
                with relay._lock:
                    relay._clients.append(q)
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp2t")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    while not relay._stop.is_set():
                        try:
                            self.wfile.write(q.get(timeout=1.0))
                        except queue.Empty:
                            continue
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    with relay._lock:
                        relay._clients.remove(q)

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http.daemon_threads = True
        self._threads = [
            threading.Thread(target=self._receive, daemon=True),
            threading.Thread(target=self.http.serve_forever, daemon=True),
        ]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.http.server_address[1]}/preview.ts"

    def start(self) -> str:
        for t in self._threads:
            t.start()
        return self.url

    def _receive(self) -> None:
        import socket

        while not self._stop.is_set():
            try:
                data = self.udp.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            self.packets += 1
            with self._lock:
                for q in self._clients:
                    if not q.full():
                        q.put_nowait(data)

    def stop(self) -> None:
        self._stop.set()
        self.http.shutdown()
        self.http.server_close()
        self.udp.close()
