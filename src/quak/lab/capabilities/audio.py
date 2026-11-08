"""Sound on the camera: the sound cards it sees, a capture from one, and a copy of that capture on the SD card.

All of it needs the toolkit's sound modules loaded, and nothing here loads them. Without them ``list_cards``
is just empty.

A capture goes to ``/tmp``, never straight to the SD card. tinycap aimed at the card reports success and leaves
a 44-byte WAV, only the header, though other writes to the card work (my-gopro raw/31, section 6). ``record``
refuses that path and treats a file with nothing after its header as a failure, whatever tinycap exited with.
``copy_to_sd`` then checks the copy against the original, so the same loss can't pass unnoticed there either.

``/tmp`` is RAM. A capture uses it until something removes the file, and a power cycle loses it.
"""

from __future__ import annotations

import datetime as dt
import posixpath
import re
import shlex
from typing import NamedTuple

from quak.core.errors import LabError, ToolkitError
from quak.lab.capabilities import printable, run
from quak.lab.capabilities.storage import _COPY_SCRIPT, _SHA256, COPY_TIMEOUT, SD_CARD, CopyResult, _absolute
from quak.lab.models import AudioDevice
from quak.lab.remote import Remote
from quak.lab.toolkit import PACKS_DIR

TINYCAP = f"{PACKS_DIR}/core/tools/tinycap"
DEFAULT_PATH = "/tmp/quak-audio.wav"
SAVE_DIR = f"{SD_CARD}/quak/recordings"
MAX_SECONDS = 300  # 58 MB at 48 kHz stereo, out of the 82 MB /tmp the camera has
_CAPTURE_ARGS = ("-d", "0", "-c", "2", "-r", "48000", "-b", "16")
_WAV_HEADER = 44
_TIMEOUT = 30.0  # also how long a capture gets beyond its own length

_CARD_LINE = re.compile(r"\s*(\d+) \[[^\]]*\]: .*? - (.*)")
_STREAM_PATH = re.compile(r"/card(\d+)/stream0$")
_RATE_RANGE = re.compile(r"(\d+) - (\d+)")
_STANDARD_RATES = (8000, 11025, 16000, 22050, 32000, 44100, 48000, 88200, 96000, 176400, 192000)

# NUL around each path, because it is the one byte a device's name can't put in these files.
LIST_SCRIPT = r"""
cat /proc/asound/cards 2>/dev/null
for f in /proc/asound/card*/stream0; do
  [ -e "$f" ] || continue
  printf '\0%s\0' "$f"
  cat "$f" 2>/dev/null
done
true
"""

_REFUSE_FOLDER = 'if [ -d "$src" ]; then echo "$src is a folder" >&2; exit 1; fi\n'

# Takes vol and dir from above. With no card in, mkdir -p would succeed in RAM, so the mount comes first.
_MAKE_FOLDER_SCRIPT = r"""
awk -v mp="$vol" '$2==mp {found=1} END {exit !found}' /proc/mounts || { echo "$vol is not mounted" >&2; exit 1; }
mkdir -p "$dir"
"""


class Recording(NamedTuple):
    path: str
    size: int  # in bytes, header included


def _parse_rates(text: str) -> list[int]:
    """Rates as listed, or for a continuous range, the usual rates inside it, so ``48000 in rates`` is always right."""
    if span := _RATE_RANGE.search(text):
        return [rate for rate in _STANDARD_RATES if int(span[1]) <= rate <= int(span[2])]
    return [int(rate) for rate in re.findall(r"\d+", text)]


def _parse_captures(card: int, name: str, stream: str) -> list[AudioDevice]:
    """What the ``Capture:`` section of a stream file offers. The file's first line and ``Playback:`` are ignored."""
    found: list[AudioDevice] = []
    capturing = False
    for line in stream.split("\n"):
        if line in ("Capture:", "Playback:"):
            capturing = line == "Capture:"
        elif capturing:
            key, _, value = line.strip().partition(":")
            if key == "Format":
                found.append(AudioDevice(card=card, name=name, capture_format=printable(value.strip())))
            elif found and key == "Channels" and value.strip().isdecimal():
                found[-1].channels = int(value)
            elif found and key == "Rates":
                found[-1].rates = _parse_rates(value)
    return [device for index, device in enumerate(found) if device not in found[:index]]


def _parse_cards(output: bytes) -> list[AudioDevice]:
    cards, *framed = output.decode("utf-8", "replace").split("\0")
    streams = {}
    for path, text in zip(framed[0::2], framed[1::2]):
        if match := _STREAM_PATH.search(path):
            streams[int(match[1])] = text
    devices: list[AudioDevice] = []
    for line in cards.split("\n"):
        if match := _CARD_LINE.match(line):
            card, name = int(match[1]), printable(match[2])
            devices += _parse_captures(card, name, streams.get(card, "")) or [AudioDevice(card=card, name=name)]
    return devices


def list_cards(remote: Remote) -> list[AudioDevice]:
    """Every sound card, with an entry per way it can capture. Changes nothing.

    A card that can't capture, such as a speaker, still gets one entry, with an empty ``capture_format``.
    Only ``stream0`` is read, which is the PCM device ``record`` captures from, so ``device`` is always 0.
    """
    return _parse_cards(run(remote, LIST_SCRIPT, "list the camera's sound cards", _TIMEOUT).stdout)


def _capture_path(path: str) -> str:
    """A ``.wav`` directly in ``/tmp``. tinycap replaces what it is pointed at, so nothing else may be aimed at."""
    target = _absolute(path)
    if posixpath.dirname(target) != "/tmp" or not target.endswith(".wav"):
        raise LabError(f"refusing {path!r}: a capture only goes to a .wav file directly in /tmp, not the SD card")
    return target


def record(remote: Remote, card: int, seconds: int, path: str = DEFAULT_PATH) -> Recording:
    """Capture 48 kHz, 16-bit stereo from ``card``, replacing any capture already at ``path``.

    ``seconds`` above ``MAX_SECONDS`` are cut down to it. This blocks for as long as the capture takes. Raises
    ``ToolkitError`` if the core toolkit's tinycap isn't installed, and ``LabError`` if no audio came out.
    """
    if card < 0:
        raise LabError(f"{card} is not a sound card")
    if seconds < 1:
        raise LabError(f"can't record for {seconds} seconds")
    duration = min(int(seconds), MAX_SECONDS)
    target = _capture_path(path)
    if remote.exec(f"[ -x {shlex.quote(TINYCAP)} ]\n", timeout=_TIMEOUT).exit_code != 0:
        raise ToolkitError(f"tinycap is not installed at {TINYCAP}. Install the Lab's core toolkit first")
    argv = [TINYCAP, target, "-D", str(int(card)), *_CAPTURE_ARGS, "-t", str(duration)]
    script = (
        f"{shlex.join(argv)} >/dev/null || exit 1\n"
        f'ls -ldn {shlex.quote(target)} | {{ read -r x x x x n x; echo "$n"; }}\n'
    )
    doing = f"record from sound card {int(card)}"
    result = run(remote, script, doing, duration + _TIMEOUT)
    size = result.text.strip()
    if not size.isdecimal() or int(size) <= _WAV_HEADER:
        detail = printable(result.stderr.decode("utf-8", "replace").strip())
        raise LabError(f"could not {doing}: tinycap wrote no audio" + (f" ({detail})" if detail else ""))
    return Recording(target, int(size))


def _sd_path(path: str) -> str:
    target = _absolute(path)
    if target != SD_CARD and not target.startswith(f"{SD_CARD}/"):
        raise LabError(f"refusing {path!r}: this only copies onto the SD card ({SD_CARD})")
    return target


def copy_to_sd(remote: Remote, src: str, dest: str) -> CopyResult:
    """Copy one file onto the SD card with ``cp``. Never replaces anything, and refuses if no card is mounted.

    The file takes its real name only once its sha256 matches the source's. ``dest`` may be an existing folder
    to copy into. Its parent has to exist. The source stays where it is.
    """
    source = _absolute(src)
    target = _sd_path(dest)
    # vol has the shared script check that the card is mounted. Without one, the copy would land in RAM and look fine.
    variables = f"src={shlex.quote(source)}\ndst={shlex.quote(target)}\nvol={shlex.quote(SD_CARD)}\n"
    doing = f"copy {source} to {target}"
    result = run(remote, variables + _REFUSE_FOLDER + _COPY_SCRIPT, doing, COPY_TIMEOUT)
    fields = dict(line.partition("=")[::2] for line in result.text.split("\n"))
    final = posixpath.join(target, posixpath.basename(source)) if "into" in fields else target
    sha256 = fields.get("source", "")
    if not _SHA256.fullmatch(sha256) or sha256 != fields.get("copy"):
        raise LabError(f"could not {doing}: its checksum could not be verified. Don't rely on {final}")
    return CopyResult(final, sha256)


def save(remote: Remote, card: int, seconds: int, dest_dir: str = SAVE_DIR) -> dict[str, object]:
    """Capture from ``card`` and keep it on the SD card, named after this computer's clock when the capture starts.

    The folder comes first, so that a card that isn't there costs nothing: the copy would only refuse once the
    capture was over. ``size`` is the capture's, which the copy's checksum matched. The capture stays in ``/tmp``
    until the next one replaces it.
    """
    folder = _sd_path(dest_dir)
    variables = f"vol={shlex.quote(SD_CARD)}\ndir={shlex.quote(folder)}\n"
    run(remote, variables + _MAKE_FOLDER_SCRIPT, f"create {folder}", _TIMEOUT)
    name = dt.datetime.now().strftime("quak-%Y%m%d-%H%M%S.wav")
    recording = record(remote, card, seconds)
    copied = copy_to_sd(remote, recording.path, posixpath.join(folder, name))
    return {"path": copied.target, "size": recording.size}
