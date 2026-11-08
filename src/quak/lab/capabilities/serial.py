"""The camera's serial ports, found in /dev and described from sysfs in one command. Nothing here changes anything.

The script only asks whether a port exists and never opens it, because opening one can reset what's on the other end:
an Arduino restarts when DTR drops. ``cd`` and ``pwd -P`` rather than readlink, as in ``quak.lab.devices``: they are shell
builtins, so no BusyBox applet has to be there.

A USB port's ids are on the USB device, some way above the port in sysfs and at a different height for each driver, so
the script climbs until it finds them. A port that isn't on USB climbs to the top and has none. The GUI shows a command
built from a device's name, so only plain names are listed.
"""

from __future__ import annotations

import re
import shlex

from quak.lab.capabilities import printable, run
from quak.lab.models import SerialPort
from quak.lab.remote import Remote

MICROCOM_SPEED = 115200  # microcom's own default is 9600, which few devices use now
_TIMEOUT = 30.0

LIST_SCRIPT = r"""
for dev in /dev/ttyUSB* /dev/ttyACM* /dev/ttyS*; do
  [ -e "$dev" ] || continue
  name=${dev##*/}
  printf 'port=%s\n' "$name"
  v=$(cd "/sys/class/tty/$name/device/driver" 2>/dev/null && pwd -P)
  [ -z "$v" ] || printf 'driver=%s\n' "${v##*/}"
  d=$(cd "/sys/class/tty/$name/device" 2>/dev/null && pwd -P)
  while [ -n "$d" ]; do
    if [ -r "$d/idVendor" ]; then
      vid= pid=
      IFS= read -r vid 2>/dev/null < "$d/idVendor"
      IFS= read -r pid 2>/dev/null < "$d/idProduct"
      printf 'usb_ids=%s:%s\n' "$vid" "$pid"
      break
    fi
    d=${d%/*}
  done
done
"""

_NAME = re.compile(r"tty(?:USB|ACM|S)[A-Za-z0-9]*")
_USB_IDS = re.compile(r"[0-9a-f]{4}:[0-9a-f]{4}")


def _parse(text: str) -> list[SerialPort]:
    ports: list[SerialPort] = []
    current: SerialPort | None = None
    for line in text.split("\n"):  # \n alone: str.splitlines would also break on other control characters
        key, separator, raw = line.partition("=")
        if not separator:
            continue
        value = printable(raw).strip()
        if key == "port":
            current = None
            if _NAME.fullmatch(value):
                current = SerialPort(device=f"/dev/{value}")
                ports.append(current)
        elif current is None:
            continue
        elif key == "driver":
            current.driver = value
        elif key == "usb_ids" and _USB_IDS.fullmatch(value):
            current.usb_ids = value
    return ports


def list_ports(remote: Remote) -> list[SerialPort]:
    """Every port in /dev, USB ones first, none being a normal answer. A lost link raises ``NetworkError``."""
    return _parse(run(remote, LIST_SCRIPT, "list the camera's serial ports", _TIMEOUT).text)


def microcom_command(device: str) -> str:
    """The command that opens ``device``, for someone to run in a terminal on the camera. Nothing here runs it."""
    return shlex.join(["microcom", "-s", str(MICROCOM_SPEED), device])
