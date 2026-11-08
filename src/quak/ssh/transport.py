"""Find the camera for SSH: auto, the camera's Wi-Fi AP, its USB-NCM gadget, or a host/IP.

The USB path is the camera's own CDC-NCM interface (USB vendor 2672), not a hub: a hub or
Ethernet adapter is just another network path and belongs in ``transport=host``.
"""

from __future__ import annotations

import re
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from quak.core.errors import NetworkError
from quak.ssh.models import Endpoint, SshProfile

CAMERA_USB_VID = "2672"
AP_HOST = "10.5.5.9"


@dataclass(frozen=True)
class UsbNet:
    interface: str
    ipv4: str
    vid: str
    pid: str


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _usb_ids(device: Path) -> tuple[str, str]:
    """Walk from a net device up to the USB device that owns it."""
    current = device.resolve()
    for _ in range(8):
        vid = _read(current / "idVendor")
        if vid:
            return vid, _read(current / "idProduct")
        if current.parent == current:
            break
        current = current.parent
    return "", ""


def _ipv4(interface: str) -> str:
    try:
        proc = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "dev", interface],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)/", proc.stdout)
    return match.group(1) if match else ""


def find_usb_nets(
    sys_class_net: Path = Path("/sys/class/net"),
    *,
    ipv4: Callable[[str], str] | None = None,
) -> list[UsbNet]:
    """Camera USB-NCM interfaces in ``sys_class_net``.

    ``ipv4`` looks up an interface's address; it defaults to the ``ip`` tool but can be
    injected (or the module ``_ipv4`` patched) so tests need no real interfaces or root.
    """
    lookup = ipv4 if ipv4 is not None else _ipv4
    found: list[UsbNet] = []
    try:
        entries = sorted(sys_class_net.iterdir())
    except OSError:
        return found
    for entry in entries:
        device = entry / "device"
        if not device.exists():
            continue
        vid, pid = _usb_ids(device)
        if vid != CAMERA_USB_VID:
            continue
        address = lookup(entry.name)
        if address:
            found.append(UsbNet(interface=entry.name, ipv4=address, vid=vid, pid=pid))
    return found


def camera_ip_from_serial(serial: str | None) -> str | None:
    """The camera's own USB-NCM address, derived the way ether_ip_setup.sh does it."""
    if not serial:
        return None
    serial = serial.strip()
    if len(serial) < 14:
        return None
    first, second = serial[11], serial[12:14]
    if not (first.isdigit() and second.isdigit()):
        return None
    return f"172.2{first}.1{second}.51"


def camera_ip_from_host(host_ip: str) -> str:
    parts = host_ip.split(".")
    if len(parts) != 4:
        raise ValueError(f"not an IPv4 address: {host_ip!r}")
    return ".".join(parts[:3] + ["51"])


def is_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _usb_endpoint(
    profile: SshProfile,
    serial: str | None,
    *,
    find_nets: Callable[[], list[UsbNet]],
    reachable: Callable[[str, int], bool],
) -> Endpoint | None:
    for net in find_nets():
        candidates = [c for c in (camera_ip_from_serial(serial), camera_ip_from_host(net.ipv4)) if c]
        for candidate in dict.fromkeys(candidates):
            if reachable(candidate, profile.http_port) or reachable(candidate, 8080):
                return Endpoint(
                    host=candidate,
                    ssh_port=profile.ssh_port,
                    http_port=profile.http_port,
                    transport="usb",
                    interface=net.interface,
                )
    return None


def resolve(
    profile: SshProfile,
    *,
    serial: str | None = None,
    probe_custom_host: bool = True,
    reachable: Callable[[str, int], bool] | None = None,
    find_nets: Callable[[], list[UsbNet]] | None = None,
) -> Endpoint:
    """Turn a profile into an endpoint. Wi-Fi is returned as 10.5.5.9; joining is the caller's job.

    ``reachable`` (the TCP probe) and ``find_nets`` (the USB-NCM scan) default to the module's
    socket/sysfs helpers but can be injected so the precedence logic is testable without hardware.
    """
    probe = reachable if reachable is not None else is_reachable
    scan = find_nets if find_nets is not None else find_usb_nets
    if profile.transport == "host":
        if not profile.host:
            raise NetworkError("this SSH profile has no host: set one (USB-NCM, Ethernet or LAN)")
        return Endpoint(profile.host, profile.ssh_port, profile.http_port, "host")
    if profile.transport == "usb":
        endpoint = _usb_endpoint(profile, serial, find_nets=scan, reachable=probe)
        if endpoint is None:
            raise NetworkError(
                "no camera USB network found. Plug the camera in directly, or use a host/IP for "
                "USB-NCM, Ethernet or another network path"
            )
        return endpoint
    if profile.transport == "wifi":
        return Endpoint(AP_HOST, profile.ssh_port, profile.http_port, "wifi")
    # auto: an explicit host wins when it answers, then the camera's own USB-NCM, then Wi-Fi.
    if profile.host and (not probe_custom_host or probe(profile.host, profile.ssh_port)):
        return Endpoint(profile.host, profile.ssh_port, profile.http_port, "host")
    endpoint = _usb_endpoint(profile, serial, find_nets=scan, reachable=probe)
    if endpoint is not None:
        return endpoint
    return Endpoint(AP_HOST, profile.ssh_port, profile.http_port, "wifi")
