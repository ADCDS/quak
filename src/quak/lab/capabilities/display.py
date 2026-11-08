"""The camera's framebuffers, read from sysfs in one command. Nothing here changes anything.

``fb*`` also finds ``fbcon``, the console that sits on top of them, so the parser only takes ``fb`` and a number. A
framebuffer's name is whatever its driver chose, so the script keeps its first line and the parser keeps what's printable.
"""

from __future__ import annotations

import re

from quak.lab.capabilities import printable, run
from quak.lab.models import Framebuffer
from quak.lab.remote import Remote

GRAPHICS = "/sys/class/graphics"
_TIMEOUT = 30.0

LIST_SCRIPT = f"""\
for d in {GRAPHICS}/fb*; do
  [ -e "$d" ] || continue
  printf 'entry=%s\\n' "${{d##*/}}"
  for f in name virtual_size bits_per_pixel state; do
    [ -r "$d/$f" ] || continue
    v=
    IFS= read -r v 2>/dev/null < "$d/$f"
    printf '%s=%s\\n' "$f" "$v"
  done
done
"""

_FRAMEBUFFER = re.compile(r"fb\d+")
_STATES = {"0": "running", "1": "suspended"}  # FBINFO_STATE_RUNNING and FBINFO_STATE_SUSPENDED in the kernel's fb.h


def _framebuffer(fields: dict[str, str]) -> Framebuffer:
    bits, state = fields.get("bits_per_pixel", ""), fields.get("state", "")
    return Framebuffer(
        name=fields.get("name", ""),
        virtual_size=fields.get("virtual_size", "").replace(",", "x"),  # sysfs has 1920,1080
        bits_per_pixel=int(bits) if bits.isdecimal() else 0,
        state=_STATES.get(state, state),
    )


def _parse(text: str) -> list[Framebuffer]:
    records: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in text.split("\n"):  # \n alone: str.splitlines would also break on other control characters
        key, separator, raw = line.partition("=")
        if not separator:
            continue
        value = printable(raw).strip()
        if key == "entry":
            current = {} if _FRAMEBUFFER.fullmatch(value) else None
            if current is not None:
                records.append(current)
        elif current is not None:
            current[key] = value
    return [_framebuffer(fields) for fields in records]


def list_framebuffers(remote: Remote) -> list[Framebuffer]:
    """Every framebuffer the kernel has, none being a normal answer. A lost link raises ``NetworkError``."""
    return _parse(run(remote, LIST_SCRIPT, "list the camera's framebuffers", _TIMEOUT).text)
