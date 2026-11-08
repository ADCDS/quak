"""How the camera is doing: uptime, load, memory, temperatures and SD card space, its kernel log and its processes.

Nothing here changes anything.
"""

from __future__ import annotations

import re
from typing import Any

from quak.core.errors import LabError
from quak.lab.capabilities import printable, run
from quak.lab.capabilities.storage import SD_CARD
from quak.lab.remote import Remote

_TIMEOUT = 30.0
_KIB = 1024
_MEM_FIELDS = {"MemTotal": "total", "MemFree": "free", "MemAvailable": "available"}

# -P keeps df on one line per filesystem, however long the device name.
STATUS_SCRIPT = f"""\
echo "uptime=$(cat /proc/uptime 2>/dev/null)"
echo "loadavg=$(cat /proc/loadavg 2>/dev/null)"
awk '/^(MemTotal|MemFree|MemAvailable):/ {{print "mem=" $1 $2}}' /proc/meminfo 2>/dev/null
for z in /sys/class/thermal/thermal_zone*; do
  [ -e "$z" ] || continue
  echo "thermal=${{z##*/}}:$(cat "$z/temp" 2>/dev/null):$(cat "$z/type" 2>/dev/null)"
done
df -P -k {SD_CARD} 2>/dev/null | awk 'NR>1 {{print "df=" $0}}'
true
"""

# Found by its numbers, not by column: the device's name comes first and can hold spaces.
_DF_LINE = re.compile(r"(\d+)\s+(\d+)\s+(\d+)\s+(\d+)%\s+(\S.*)$")


def _floats(text: str, count: int) -> list[float]:
    """The first ``count`` numbers in ``text``, or nothing if one is missing or isn't a number."""
    try:
        values = [float(word) for word in text.split()[:count]]
    except ValueError:
        return []
    return values if len(values) == count else []


def _parse_status(text: str) -> dict[str, Any]:
    status: dict[str, Any] = {"uptime": None, "load": [], "mem": {}, "temp": [], "df": {}}
    for line in text.split("\n"):
        key, _, value = line.partition("=")
        if key == "uptime":
            status["uptime"] = next(iter(_floats(value, 1)), None)
        elif key == "loadavg":
            status["load"] = _floats(value, 3)
        elif key == "mem":
            name, _, kib = value.partition(":")
            if name in _MEM_FIELDS and kib.isdecimal():
                status["mem"][_MEM_FIELDS[name]] = int(kib) * _KIB
        elif key == "thermal":
            zone, _, rest = value.partition(":")
            millidegrees, _, kind = rest.partition(":")
            for reading in _floats(millidegrees, 1):  # a sensor can fail to read, and then it isn't listed
                status["temp"].append({"zone": zone, "type": printable(kind), "celsius": reading / 1000})
        elif key == "df" and (match := _DF_LINE.search(value)):
            status["df"] = {
                "path": match[5],
                "total": int(match[1]) * _KIB,
                "used": int(match[2]) * _KIB,
                "free": int(match[3]) * _KIB,
                "percent": int(match[4]),
            }
    return status


def status(remote: Remote) -> dict[str, Any]:
    """A snapshot, in one command. What the camera can't say stays empty, which isn't an error.

    ``uptime``: seconds, or None. ``load``: the 1, 5 and 15 minute averages. ``mem``: ``total``, ``free`` and
    ``available`` in bytes. ``temp``: a dict per thermal zone with ``zone``, ``type`` and ``celsius``.
    ``df``: the SD card's ``path``, ``total``, ``used`` and ``free`` in bytes, and ``percent``.
    """
    return _parse_status(run(remote, STATUS_SCRIPT, "read the camera's status", _TIMEOUT).text)


def _text(remote: Remote, command: str, doing: str) -> str:
    result = run(remote, command, doing, _TIMEOUT)
    if not result.stdout and result.stderr:  # a pipeline exits as its last command did, so a failed dmesg shows here
        raise LabError(f"could not {doing}: {printable(result.stderr.decode('utf-8', 'replace').strip())}")
    return printable(result.text, keep="\n\t")


def logs(remote: Remote, tail: int = 200) -> str:
    """The last ``tail`` lines of the kernel log."""
    if tail < 1:
        raise LabError(f"can't show the last {tail} lines of a log")
    return _text(remote, f"dmesg | tail -n {int(tail)}", "read the camera's kernel log")


def processes(remote: Remote) -> str:
    """What ``ps`` prints."""
    return _text(remote, "ps", "list the camera's processes")
