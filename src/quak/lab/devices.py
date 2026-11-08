"""What is on the camera's USB bus, read from sysfs in one remote command. Nothing here changes anything.

``/sys/bus/usb/devices`` holds devices (``usb1``, ``1-1``, ``1-1.2``) and their interfaces (``1-1:1.0``)
side by side, each printed as an ``entry=`` line followed by ``field=value`` lines. The result is a
flat list: the id already spells out a device's bus and port path.

``product`` and ``manufacturer`` are whatever the plugged-in device chose to say, so they can't be
trusted to stay on one line or to be fit for a terminal. The script keeps only their first line, the
parser splits on ``\\n`` alone (``str.splitlines`` would also break on ``\\r`` and ``\\u2028``, and let a
device forge a record), and non-printable characters are dropped.
"""

from __future__ import annotations

import re

from quak.core.errors import LabError
from quak.lab.hostmode import USB_DEVICES
from quak.lab.models import UsbDevice, UsbInterface
from quak.lab.remote import Remote

_TIMEOUT = 30.0

# ``cd`` and ``pwd -P`` rather than readlink: they are shell builtins, so no BusyBox applet has to be there.
LIST_SCRIPT = f"""\
for d in {USB_DEVICES}/*; do
  [ -e "$d" ] || continue
  printf 'entry=%s\\n' "${{d##*/}}"
  for f in idVendor idProduct product manufacturer bDeviceClass speed bInterfaceClass; do
    [ -r "$d/$f" ] || continue
    v=
    IFS= read -r v 2>/dev/null < "$d/$f"
    printf '%s=%s\\n' "$f" "$v"
  done
  v=$(cd "$d/driver" 2>/dev/null && pwd -P)
  [ -z "$v" ] || printf 'driver=%s\\n' "${{v##*/}}"
done
"""

_DEVICE_ID = re.compile(r"usb\d+|\d+-\d+(?:\.\d+)*")
_INTERFACE_ID = re.compile(r"(\d+)-(\d+(?:\.\d+)*):\d+\.\d+")

_DEVICE_FIELDS = {
    "idVendor": "vid",
    "idProduct": "pid",
    "product": "product",
    "manufacturer": "manufacturer",
    "bDeviceClass": "bclass",
    "speed": "speed",
    "driver": "driver",
}
_INTERFACE_FIELDS = {"bInterfaceClass": "iclass", "driver": "driver"}

_CLASS_KINDS = {
    "01": "audio",
    "02": "network",
    "03": "hid",
    "08": "storage",
    "09": "hub",
    "0a": "network",
    "0d": "network",  # as the Lab brief has it; USB-IF calls this class Content Security, and NCM's 0d is a subclass
    "0e": "video",
    "ff": "vendor",
}

# A bound driver knows more than the class code: r8152 Ethernet and every common USB-serial chip say
# only "vendor-specific" (ff), and cdc_acm serial shares class 02 with CDC Ethernet.
_DRIVER_KINDS = {
    "snd_usb_audio": "audio",
    "usb_storage": "storage",
    "uas": "storage",
    "usbhid": "hid",
    "uvcvideo": "video",
    "hub": "hub",
    **dict.fromkeys(
        (
            "asix",
            "ax88179_178a",
            "cdc_eem",
            "cdc_ether",
            "cdc_mbim",
            "cdc_ncm",
            "dm9601",
            "lan78xx",
            "qmi_wwan",
            "r8152",
            "rndis_host",
            "smsc95xx",
        ),
        "network",
    ),
    **dict.fromkeys(("cdc_acm", "ch341", "cp210x", "ftdi_sio", "option", "pl2303", "usbserial_generic"), "serial"),
}

# A composite device gets one label, so what it is mainly for has to outrank its extras: a webcam's
# microphone, a headset's buttons, a modem's serial ports and driver CD, a gaming mouse's vendor interface.
_PRECEDENCE = ("hub", "video", "audio", "network", "storage", "serial", "hid", "vendor")


def _clean(value: str) -> str:
    return "".join(ch for ch in value if ch.isprintable()).strip()


def _parse(text: str) -> list[UsbDevice]:
    """Anything that isn't a field of a well-named entry is skipped, along with the fields of an entry that isn't."""
    devices: dict[str, UsbDevice] = {}
    interfaces: list[tuple[str, UsbInterface]] = []
    current: UsbDevice | UsbInterface | None = None
    fields: dict[str, str] = {}
    for line in text.split("\n"):
        key, separator, raw = line.partition("=")
        if not separator:
            continue
        value = _clean(raw)
        if key != "entry":
            if current is not None and key in fields:
                setattr(current, fields[key], value)
            continue
        current = None
        if _DEVICE_ID.fullmatch(value):
            current = devices.setdefault(value, UsbDevice(id=value))
            fields = _DEVICE_FIELDS
        elif match := _INTERFACE_ID.fullmatch(value):
            bus, port = match.groups()
            current = UsbInterface(id=value)
            fields = _INTERFACE_FIELDS
            interfaces.append((f"usb{bus}" if port == "0" else f"{bus}-{port}", current))  # 1-0:1.0 is usb1's
    # Afterwards, because a root hub's interface sorts ahead of it. A device unplugged mid-read can leave orphans.
    for parent, interface in interfaces:
        if parent in devices:
            devices[parent].interfaces.append(interface)
    return list(devices.values())


def list_devices(remote: Remote) -> list[UsbDevice]:
    """Every device on the bus, root hubs included, and none outside host mode. A lost link raises ``NetworkError``."""
    result = remote.exec(LIST_SCRIPT, timeout=_TIMEOUT)
    if result.exit_code != 0:
        detail = result.stderr.decode("utf-8", "replace").strip() or f"exit code {result.exit_code}"
        raise LabError(f"could not list the camera's USB devices: {detail}")
    return _parse(result.text)


def _interface_kind(interface: UsbInterface) -> str:
    return _DRIVER_KINDS.get(interface.driver.replace("-", "_")) or _CLASS_KINDS.get(interface.iclass, "other")


def classify(device: UsbDevice) -> str:
    """One of audio, storage, network, hid, video, hub, serial, vendor or other.

    The interfaces decide. ``bDeviceClass`` is only for when they say nothing: it is usually 00 or ef,
    and an Arduino's 02 would call a serial port a network card.
    """
    kinds = {_interface_kind(interface) for interface in device.interfaces} - {"other"}
    if not kinds:
        kinds = {_CLASS_KINDS.get(device.bclass, "other")}
    return next((kind for kind in _PRECEDENCE if kind in kinds), "other")
