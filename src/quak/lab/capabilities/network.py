"""The camera's network interfaces: list them, and give one an address by DHCP or by hand.

``list_interfaces`` changes nothing. ``configure`` acts at once, on whatever interface it is told to, and nothing
here knows which one the Lab's own link runs over. Configuring that one can cut the link, and then the call raises
``NetworkError`` even though the change went through.

A static setup flushes the interface's addresses, and that can't be taken back. So everything that can be refused
is refused before a command changes anything, including what ``ip`` would only reject after the flush, like a
gateway outside the subnet. The address and route last until the camera restarts.

An interface name is one the kernel allows, which is nearly any bytes, so it counts as data: only plain names are
listed or accepted, and every value in a script is quoted as well.
"""

from __future__ import annotations

import ipaddress
import re
import shlex
from collections.abc import Sequence

from quak.core.errors import LabError
from quak.lab.capabilities import printable, run
from quak.lab.models import NetworkInterface
from quak.lab.remote import Remote

RESOLV_CONF = "/etc/resolv.conf"
_LOOPBACK_NAME = "lo"
_TIMEOUT = 30.0
_CONFIGURE_TIMEOUT = 60.0  # udhcpc -n gives up by itself, by default within about ten seconds

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,14}")  # 15 is the kernel's limit. The first character rules out -x
_LINK_LINE = re.compile(r"\d+:\s+([^\s:@]+)(?:@\S+)?:\s+<([^>]*)>(.*)")
_ADDR_LINE = re.compile(r"\d+:\s+([^\s:@]+)(?:@\S+)?\s+inet\s+([0-9.]+(?:/\d+)?)")
_HARDWARE = re.compile(r"\blink/(\S+)(?:\s+((?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2})(?!\S))?")

# ip stops the script if it fails, because otherwise a camera without ip would look like one with no interfaces.
LIST_SCRIPT = r"""
ip -o link show || exit 1
ip -4 -o addr show || exit 1
for d in /sys/class/net/*; do
  [ -d "$d" ] || continue
  printf 'net=%s\n' "${d##*/}"
  driver=$(readlink "$d/device/driver" 2>/dev/null)
  printf 'driver=%s\n' "${driver##*/}"
  bus=$(readlink "$d/device/subsystem" 2>/dev/null)
  printf 'bus=%s\n' "${bus##*/}"
  if [ -e "$d/wireless" ] || [ -e "$d/phy80211" ]; then echo wireless=1; fi
done
true
"""

_FAIL = "fail() { printf '%s\\n' \"$1\" >&2; exit 1; }\n"


def _classify(name: str, link: str, bus: str, wireless: bool) -> str:
    """``ethernet``, ``wifi``, ``usb`` or ``other``.

    An Ethernet adapter on USB is ``ethernet`` and a Wi-Fi stick is ``wifi``, because what they connect to matters
    more than how they are plugged in. The kernel's usbnet draws the same line: it names an adapter ``eth0``, and
    keeps ``usb0`` for a link that is only USB. A bridge or a dummy looks like Ethernet but has no device behind it.
    """
    if wireless or name.startswith("wl"):
        return "wifi"
    if name.startswith("usb"):
        return "usb"
    if link == "ether" and name.startswith(("eth", "en")):
        return "ethernet"
    if bus == "usb":
        return "usb"
    if link == "ether" and bus:
        return "ethernet"
    return "other"


def _parse(text: str) -> tuple[list[NetworkInterface], set[str]]:
    """The interfaces ``ip`` lists, and which of them are loopback, which ``kind`` has no word for."""
    interfaces: dict[str, NetworkInterface] = {}
    link_types: dict[str, str] = {}
    loopback: set[str] = set()
    addresses: dict[str, list[str]] = {}
    sysfs: dict[str, dict[str, str]] = {}
    current: dict[str, str] = {}
    for line in text.split("\n"):  # \n alone: str.splitlines would also break on other control characters
        if link := _LINK_LINE.match(line):
            name, flags = link[1], link[2].split(",")
            if not _NAME.fullmatch(name):
                continue
            hardware = _HARDWARE.search(link[3])
            link_types[name] = hardware[1] if hardware else ""
            if "LOOPBACK" in flags or link_types[name] == "loopback":
                loopback.add(name)
            mac = hardware[2] if hardware and hardware[2] else ""
            interfaces[name] = NetworkInterface(name=name, mac=mac.lower(), up="UP" in flags)
        elif addr := _ADDR_LINE.match(line):
            addresses.setdefault(addr[1], []).append(addr[2])
        elif line.startswith("net="):
            current = sysfs.setdefault(line.removeprefix("net="), {})
        else:
            key, _, value = line.partition("=")
            current[key] = value
    for name, interface in interfaces.items():
        fields = sysfs.get(name, {})
        interface.addresses = addresses.get(name, [])
        interface.driver = printable(fields.get("driver", ""))
        if name not in loopback:
            interface.kind = _classify(name, link_types[name], fields.get("bus", ""), "wireless" in fields)
    return list(interfaces.values()), loopback


def _survey(remote: Remote) -> tuple[list[NetworkInterface], set[str]]:
    return _parse(run(remote, LIST_SCRIPT, "list the camera's network interfaces", _TIMEOUT).text)


def list_interfaces(remote: Remote) -> list[NetworkInterface]:
    """Every interface, loopback included, in ``ip``'s order. Changes nothing."""
    return _survey(remote)[0]


def _static_address(address: str, prefix: int) -> ipaddress.IPv4Interface:
    try:
        interface = ipaddress.IPv4Interface(f"{address}/{prefix}")
    except ValueError as error:
        raise LabError(f"{address!r} with prefix {prefix!r} is not an IPv4 address") from error
    ip = interface.ip
    if not interface.network.prefixlen or ip.is_unspecified or ip.is_loopback or ip.is_multicast:
        raise LabError(f"refusing {interface}: an interface can't use that address")
    return interface


def _gateway(gateway: str, interface: ipaddress.IPv4Interface) -> str:
    try:
        router = ipaddress.IPv4Address(gateway)
    except ValueError as error:
        raise LabError(f"{gateway!r} is not an IPv4 gateway") from error
    if router == interface.ip or router not in interface.network:
        raise LabError(f"refusing gateway {router}: {interface} can't reach it")
    return str(router)


def _dns_servers(dns: str | Sequence[str]) -> list[str]:
    servers = [dns] if isinstance(dns, str) else list(dns)
    if not servers:
        raise LabError(f"refusing an empty DNS list: it would leave {RESOLV_CONF} with no servers")
    try:
        return [str(ipaddress.ip_address(server)) for server in servers]
    except ValueError as error:
        raise LabError(f"{dns!r} is not a list of DNS server addresses") from error


def _step(argv: Sequence[str], failure: str) -> str:
    return f"{shlex.join(argv)} || fail {shlex.quote(failure)}\n"


def configure(
    remote: Remote,
    name: str,
    dhcp: bool = True,
    address: str | None = None,
    prefix: int = 24,
    gateway: str | None = None,
    dns: str | Sequence[str] | None = None,
) -> dict[str, object]:
    """Bring an interface up and give it an address, by DHCP or, with ``dhcp=False``, the ``address`` given.

    Refuses loopback, and an interface the camera doesn't list. ``/etc/resolv.conf`` is only written when ``dns``
    is given. The steps stop at the first failure, whose ``LabError`` says what state that left.

    Returns ``interface``, ``mode`` (``dhcp`` or ``static``), ``addresses`` as read back from the camera, and the
    ``gateway`` and ``dns`` this call set. Those two are ``None`` when it set none, so under DHCP they don't say
    what the lease brought.
    """
    if not _NAME.fullmatch(name):
        raise LabError(f"{name!r} is not a network interface")
    if name == _LOOPBACK_NAME:
        raise LabError(f"refusing {name}: it is the loopback interface")
    servers = None if dns is None else _dns_servers(dns)
    target = None
    router = None
    if dhcp:
        if address is not None or gateway is not None:
            raise LabError("DHCP chooses the address and gateway. Pass dhcp=False to set them by hand")
        setup = _step(["udhcpc", "-i", name, "-q", "-n"], f"no DHCP lease on {name}")
    else:
        if address is None:
            raise LabError("a static setup needs an address")
        target = _static_address(address, prefix)
        router = None if gateway is None else _gateway(gateway, target)
        setup = _step(["ip", "addr", "flush", "dev", name], f"could not clear the addresses on {name}")
        setup += _step(["ip", "addr", "add", target.with_prefixlen, "dev", name], f"{name} now has no address")
        if router is not None:
            route = ["ip", "route", "add", "default", "via", router, "dev", name]
            setup += _step(route, f"{name} has its address, but no default route")

    interfaces, loopback = _survey(remote)
    if name in loopback:
        raise LabError(f"refusing {name}: it is the loopback interface")
    if name not in {interface.name for interface in interfaces}:
        raise LabError(f"refusing {name}: the camera has no such interface")

    script = _FAIL + _step(["ip", "link", "set", "dev", name, "up"], f"could not bring {name} up") + setup
    if servers is not None:
        write = shlex.join(["printf", "nameserver %s\\n", *servers]) + f" > {shlex.quote(RESOLV_CONF)}"
        failure = f"{name} is set up, but {RESOLV_CONF} could not be written"
        script += f"{write} || fail {shlex.quote(failure)}\n"
    script += shlex.join(["ip", "-4", "-o", "addr", "show", "dev", name]) + " || true\n"
    result = run(remote, script, f"configure {name}", _CONFIGURE_TIMEOUT)

    matches = (_ADDR_LINE.match(line) for line in result.text.split("\n"))
    addresses = [match[2] for match in matches if match and match[1] == name]
    if target is not None and target.with_prefixlen not in addresses:
        raise LabError(f"{name} does not show {target.with_prefixlen} after setting it")
    if not addresses:
        raise LabError(f"udhcpc finished, but {name} has no address. The camera may lack udhcpc's default script")
    return {
        "interface": name,
        "mode": "dhcp" if dhcp else "static",
        "addresses": addresses,
        "gateway": router,
        "dns": servers,
    }
