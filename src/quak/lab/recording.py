"""Put a capture under a clip, with ffmpeg on this computer. Nothing here touches the camera.

The video is copied as it is and only the sound is encoded, so the picture loses nothing. ``-shortest`` ends the result
with whichever ends first, so a capture shorter than its clip cuts the clip short.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
from pathlib import Path

from quak.core.errors import LabError
from quak.lab.capabilities import printable

_ERROR_TAIL = 500  # ffmpeg can repeat itself for every bad frame of a damaged file


def ffmpeg_path() -> str | None:
    """The ffmpeg this computer would run, if it has one."""
    return shutil.which("ffmpeg")


def _mix_graph(offset: float) -> str:
    """Both soundtracks as one. amix lines its inputs up by sample, not timestamp, so the offset goes in the graph."""
    if offset > 0:
        delayed = f"[1:a]adelay=delays={round(offset * 1000)}:all=1[wav];"
    elif offset < 0:
        delayed = f"[1:a]atrim=start={-offset:f},asetpts=PTS-STARTPTS[wav];"
    else:
        return "[0:a][1:a]amix=inputs=2[mixed]"
    return f"{delayed}[0:a][wav]amix=inputs=2[mixed]"


def _argv(ffmpeg: str, clip: Path, wav: Path, out: Path, offset: float, mix: bool) -> list[str]:
    if mix:
        sound = ["-i", str(wav), "-filter_complex", _mix_graph(offset), "-map", "0:v", "-map", "[mixed]"]
    else:  # -itsoffset applies to the input that follows it, so it has to come before the WAV's -i
        shift = ["-itsoffset", f"{offset:f}"] if offset else []
        sound = [*shift, "-i", str(wav), "-map", "0:v", "-map", "1:a"]
    quiet = ["-hide_banner", "-loglevel", "error"]  # leaves only errors on stderr, which is what a failure reports
    return [ffmpeg, *quiet, "-y", "-i", str(clip), *sound, "-c:v", "copy", "-c:a", "aac", "-shortest", str(out)]


def merge(
    clip: str | os.PathLike[str],
    wav: str | os.PathLike[str],
    out: str | os.PathLike[str],
    offset_seconds: float = 0.0,
    mix: bool = False,
) -> Path:
    """Write ``out``, with the video of ``clip`` and the sound of ``wav``, or with ``mix``, the sound of both.

    A positive ``offset_seconds`` starts the WAV that much later against the clip, a negative one that much earlier.
    Returns the absolute path written. Raises ``LabError`` if an input is missing, ``out`` already exists, ffmpeg isn't
    installed or ffmpeg fails. A failed merge leaves no file behind.
    """
    # Absolute, so ffmpeg can't take a name for an option (-x.mp4) or a protocol (pipe:1).
    source, capture, target = (Path(path).absolute() for path in (clip, wav, out))
    for path in (source, capture):
        if not path.is_file():
            raise LabError(f"can't merge: {path} is not a file")
    if not math.isfinite(offset_seconds):
        raise LabError(f"can't merge: {offset_seconds} is not an offset in seconds")
    ffmpeg = ffmpeg_path()
    if ffmpeg is None:
        raise LabError("can't merge: ffmpeg is not installed on this computer")
    argv = _argv(ffmpeg, source, capture, target, offset_seconds, mix)
    try:
        target.touch(exist_ok=False)  # takes the name first, so ffmpeg's -y can only ever replace this empty file
    except FileExistsError:
        raise LabError(f"refusing to overwrite {target}") from None
    except OSError as exc:
        raise LabError(f"can't write {target}: {exc.strerror}") from None
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True)
        if done.returncode != 0:
            detail = printable(done.stderr.decode("utf-8", "replace").strip(), keep="\n")[-_ERROR_TAIL:]
            raise LabError(f"ffmpeg could not merge {source.name}: {detail or f'exit code {done.returncode}'}")
        if target.stat().st_size == 0:
            raise LabError(f"ffmpeg could not merge {source.name}: it wrote nothing")
    except BaseException:
        target.unlink(missing_ok=True)  # the file claimed above: left there, it would refuse the next try
        raise
    return target
