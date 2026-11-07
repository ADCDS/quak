"""The camera's HTTP API (over its Wi-Fi AP at 10.5.5.9:8080, or USB). Stdlib only.

https://gopro.github.io/OpenGoPro/http
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import http.client
import json
import socket
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from quak.core.errors import NetworkError, Rejected

CAMERA_AP_URL = "http://10.5.5.9:8080"
PREVIEW_PORT = 8554


def is_plain_name(name: str) -> bool:
    """A single file or folder name, with nothing that could point somewhere else on this computer."""
    return bool(name) and name not in (".", "..") and not any(c in name for c in "/\\:\0")


def free_path(dest: Path) -> Path:
    """`dest`, or the first free name beside it. Clip numbers can repeat, so a new clip must not replace an old one."""
    candidate, n = dest, 0
    while candidate.exists():
        n += 1
        candidate = dest.with_name(f"{dest.stem}_{n}{dest.suffix}")
    return candidate


@dataclass(frozen=True)
class MediaFile:
    folder: str
    name: str
    created: int  # camera local wall time encoded as epoch seconds ("cre")
    size: int

    @property
    def path(self) -> str:
        return f"{self.folder}/{self.name}"

    @property
    def is_video(self) -> bool:
        return self.name.upper().endswith(".MP4")


# a HERO11 sometimes stamps a clip's "cre" up to ~20 s before the shutter command; files are
# identified by not being in the baseline/previous list first, the window is the second check
WINDOW_EARLY = 45


def camera_epoch(camera_time: dt.datetime) -> int:
    """The camera stores times as local wall-clock time encoded like UTC epoch seconds ("cre")."""
    return int(camera_time.replace(tzinfo=dt.timezone.utc).timestamp())


class CameraHttp:
    def __init__(self, base_url: str = CAMERA_AP_URL, timeout: float = 10.0, source_ip: str | None = None) -> None:
        u = urllib.parse.urlsplit(base_url)
        self.host, self.port = u.hostname, u.port or 80
        self.timeout = timeout
        self.source_ip = source_ip  # bind to the Wi-Fi interface's address when several routes exist

    # ---------------------------------------------------------------- plumbing

    def _conn(self, timeout: float | None = None) -> http.client.HTTPConnection:
        source = (self.source_ip, 0) if self.source_ip else None
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout or self.timeout, source_address=source)

    def _get(self, path: str, params: dict | None = None, timeout: float | None = None) -> bytes:
        # the camera wants path=100GOPRO/GX010001.MP4 literally: an encoded slash (%2F) gets HTTP 400
        url = path + ("?" + urllib.parse.urlencode(params, safe="/") if params else "")
        conn = self._conn(timeout)
        try:
            conn.request("GET", url)
            r = conn.getresponse()
            body = r.read()
        except (OSError, http.client.HTTPException) as e:
            raise NetworkError(f"camera HTTP {url}: {e}") from None
        finally:
            conn.close()
        if r.status != 200:
            raise Rejected(f"HTTP {url}", f"{r.status} {body[:200]!r}")
        return body

    def _json(self, path: str, params: dict | None = None) -> dict:
        body = self._get(path, params)
        try:
            return json.loads(body) if body.strip() else {}
        except ValueError:
            raise NetworkError(f"camera HTTP {path}: not JSON: {body[:120]!r}") from None

    def reachable(self, timeout: float = 2.0) -> bool:
        try:
            with socket.create_connection(
                (self.host, self.port), timeout=timeout, source_address=(self.source_ip, 0) if self.source_ip else None
            ):
                pass
            self._get("/gopro/version", timeout=timeout)
            return True
        except (OSError, NetworkError, Rejected):
            return False

    # ---------------------------------------------------------------- camera

    def version(self) -> str:
        return self._json("/gopro/version").get("version", "")

    def state(self) -> dict:
        """{'status': {id: value}, 'settings': {id: value}} with integer ids."""
        raw = self._json("/gopro/camera/state")
        return {k: {int(i): v for i, v in raw.get(k, {}).items()} for k in ("status", "settings")}

    def keep_alive(self) -> None:
        self._get("/gopro/camera/keep_alive")

    def turbo(self, on: bool) -> None:
        self._get("/gopro/media/turbo_transfer", {"p": int(on)})

    def preview_start(self, port: int | None = None) -> None:
        self._get("/gopro/camera/stream/start", {"port": port} if port else None)

    def preview_stop(self) -> None:
        self._get("/gopro/camera/stream/stop")

    # ---------------------------------------------------------------- media

    def media_list(self) -> list[MediaFile]:
        out = []
        for folder in self._json("/gopro/media/list").get("media", []):
            for f in folder.get("fs", []):
                if is_plain_name(folder["d"]) and is_plain_name(f["n"]):  # names become local paths on download
                    out.append(MediaFile(folder["d"], f["n"], int(f.get("cre", 0)), int(f.get("s", 0))))
        return out

    def media_info(self, path: str) -> dict:
        return self._json("/gopro/media/info", {"path": path})

    def thumbnail(self, path: str) -> bytes:
        return self._get("/gopro/media/thumbnail", {"path": path})

    def download(
        self,
        f: MediaFile | str,
        dest: Path,
        progress: Callable[[int, int], None] | None = None,
        chunk: int = 256 * 1024,
    ) -> tuple[int, str, Path]:
        """Stream a file beside `dest`, never replacing one that exists. Returns (bytes, sha256, where it went).
        Only a complete file gets its final name: the delete dialog tells people to download first."""
        path = f.path if isinstance(f, MediaFile) else f
        conn = self._conn(timeout=60)
        digest, done = hashlib.sha256(), 0
        dest = free_path(dest)
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            conn.request("GET", "/videos/DCIM/" + urllib.parse.quote(path))
            r = conn.getresponse()
            if r.status != 200:
                raise Rejected(f"download {path}", f"HTTP {r.status}")
            total = int(r.getheader("Content-Length") or 0)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "wb") as fh:
                while block := r.read(chunk):
                    fh.write(block)
                    digest.update(block)
                    done += len(block)
                    if progress:
                        progress(done, total)
            if total and done != total:
                raise NetworkError(f"download {path}: the connection ended after {done} of {total} bytes")
            tmp.replace(dest)
        except (OSError, http.client.HTTPException) as e:
            raise NetworkError(f"download {path}: {e}") from None
        finally:
            conn.close()
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
        return done, digest.hexdigest(), dest

    def delete(self, path: str) -> None:
        """Delete ONE file. Callers decide which file; see MediaLedger.delete_own for tests."""
        self._get("/gopro/media/delete/file", {"path": path})


class MediaLedger:
    """Remembers what was on the card and when we recorded, so only our own clips get deleted."""

    def __init__(self, baseline: list[MediaFile]) -> None:
        self.baseline = {f.path for f in baseline}
        self.windows: list[tuple[int, int]] = []  # camera-local epoch seconds

    def add_window(self, start: int, end: int) -> None:
        self.windows.append((start - WINDOW_EARLY, end + 2))  # see WINDOW_EARLY

    def ours(self, files: list[MediaFile]) -> list[MediaFile]:
        return [f for f in files if f.path not in self.baseline and any(a <= f.created <= b for a, b in self.windows)]

    def delete_own(self, http_api: CameraHttp, f: MediaFile) -> None:
        if f.path in self.baseline:
            raise PermissionError(f"refusing to delete {f.path}: it was on the card before this session")
        if not any(a <= f.created <= b for a, b in self.windows):
            raise PermissionError(f"refusing to delete {f.path}: not recorded during this session")
        http_api.delete(f.path)
