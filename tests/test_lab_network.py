"""Network interfaces on the camera: no hardware, no ssh, and nothing touches a real network.

Most of this looks at the commands sent and feeds canned ``ip`` output back. That says nothing about whether the
scripts work, so ``RealShellRuns`` also runs them in a local shell, as ``test_lab_storage`` does, with temporary
paths for the camera's and shell functions standing in for ip and udhcpc. Functions rather than stubs on PATH,
because they win over a BusyBox applet too, so these tests can't reach the real ip. Whether the camera's own
BusyBox agrees is still for a real camera to show.
"""

import inspect
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from quak.core.errors import LabError, NetworkError
from quak.lab.capabilities import network
from quak.lab.models import NetworkInterface
from quak.ssh.models import ExecResult

# Same check as test_lab_hostmode: substrings on purpose, and only dd is short enough to need a word boundary.
FORBIDDEN = re.compile(r"mkfs|mke2fs|format|flash|update|\bdd\b|fdisk|parted|wipefs", re.IGNORECASE)

# Invented interfaces, in the shape BusyBox ip prints them: one line each, with \ where a line break would be.
IP_LINK = r"""1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue \    link/loopback 11:22:33:44:55:66 brd 11:22:33:44:55:66
2: wlan0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc mq \    link/ether 11:22:33:44:55:66 brd 11:22:33:44:55:66
3: usb0: <NO-CARRIER,BROADCAST,MULTICAST,UP> mtu 1500 qdisc mq \    link/ether 11:22:33:44:55:66 brd 11:22:33:44:55:66
4: eth0: <BROADCAST,MULTICAST> mtu 1500 qdisc noop \    link/ether 11:22:33:44:55:66 brd 11:22:33:44:55:66
5: wlan1: <BROADCAST,MULTICAST,LOWER_UP> mtu 1500 qdisc noop \    link/ether 11:22:33:44:55:66 brd 11:22:33:44:55:66
6: wwan0: <POINTOPOINT,NOARP,UP,LOWER_UP> mtu 1500 qdisc noop \    link/none
7: lan0: <BROADCAST,MULTICAST,UP> mtu 1500 qdisc mq \    link/ether 11:22:33:44:55:66 brd 11:22:33:44:55:66
8: dummy0: <BROADCAST,NOARP> mtu 1500 qdisc noop \    link/ether 11:22:33:44:55:66 brd 11:22:33:44:55:66
9: sit0@NONE: <NOARP> mtu 1480 qdisc noop \    link/sit 0.0.0.0 brd 0.0.0.0
"""

IP_ADDR = r"""1: lo    inet 127.0.0.1/8 scope host lo\       valid_lft forever preferred_lft forever
2: wlan0    inet 10.5.5.9/24 brd 10.5.5.255 scope global wlan0\       valid_lft forever preferred_lft forever
3: usb0    inet 172.20.145.51/24 brd 172.20.145.255 scope global usb0\       valid_lft forever preferred_lft forever
3: usb0    inet 172.20.145.52/24 scope global secondary usb0\       valid_lft forever preferred_lft forever
6: wwan0    inet 10.0.0.1 peer 10.0.0.2/32 scope global wwan0\       valid_lft forever preferred_lft forever
"""

SYSFS = """\
net=lo
driver=
bus=
net=wlan0
driver=ath6kl_sdio
bus=sdio
wireless=1
net=usb0
driver=
bus=
net=eth0
driver=r8152
bus=usb
net=wlan1
driver=rtl8xxxu
bus=usb
wireless=1
net=wwan0
driver=qmi_wwan
bus=usb
net=lan0
driver=stmmaceth
bus=platform
net=dummy0
driver=
bus=
net=sit0
driver=
bus=
"""

LISTING = IP_LINK + IP_ADDR + SYSFS

INTERFACES = [
    NetworkInterface("lo", "11:22:33:44:55:66", True, "other", ["127.0.0.1/8"], ""),
    NetworkInterface("wlan0", "11:22:33:44:55:66", True, "wifi", ["10.5.5.9/24"], "ath6kl_sdio"),
    NetworkInterface("usb0", "11:22:33:44:55:66", True, "usb", ["172.20.145.51/24", "172.20.145.52/24"], ""),
    NetworkInterface("eth0", "11:22:33:44:55:66", False, "ethernet", [], "r8152"),
    NetworkInterface("wlan1", "11:22:33:44:55:66", False, "wifi", [], "rtl8xxxu"),
    NetworkInterface("wwan0", "", True, "usb", ["10.0.0.1"], "qmi_wwan"),
    NetworkInterface("lan0", "11:22:33:44:55:66", True, "ethernet", [], "stmmaceth"),
    NetworkInterface("dummy0", "11:22:33:44:55:66", False, "other", [], ""),
    NetworkInterface("sit0", "", False, "other", [], ""),
]

BAD_NAMES = (
    "",
    "eth0; reboot",
    "$(reboot)",
    "`reboot`",
    "a b",
    "../eth0",
    "eth0/x",
    "eth0\n",
    "eth0\nreboot",
    "-i",
    "--help",
    "eth0:1",
    "eth0@if3",
    "eth0'",
    "a" * 16,
)


def answer(text: str = "", exit_code: int = 0, stderr: bytes = b"") -> ExecResult:
    return ExecResult(exit_code=exit_code, stdout=text.encode(), stderr=stderr)


def readback(*addresses: str, name: str = "eth0") -> ExecResult:
    return answer("".join(f"4: {name}    inet {address} scope global {name}\n" for address in addresses))


class FakeRemote:
    """A ``Remote`` that gives its canned answers in turn. An exception as the answer is raised."""

    def __init__(self, *replies: ExecResult | Exception) -> None:
        self.replies = list(replies)
        self.commands: list[str] = []

    def exec(self, command: str, input: bytes | None = None, timeout: float = 120.0) -> ExecResult:
        self.commands.append(command)
        if not self.replies:
            raise AssertionError(f"one command more than expected: {command}")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def read_file(self, path: str) -> bytes:
        raise AssertionError(f"network runs commands, but read {path}")

    def write_file(self, path: str, data: bytes) -> None:
        raise AssertionError(f"network runs commands, but wrote {path}")


def argvs(script: str) -> list[list[str]]:
    """Each step of a configure script as the argv a shell would give it, without the helper and the ``|| fail``."""
    lines = script.splitlines()
    assert lines[0].startswith("fail() "), lines[0]
    return [shlex.split(line.partition(" || ")[0]) for line in lines[1:]]


class ListInterfacesTests(unittest.TestCase):
    def test_parses_and_classifies_every_interface_in_ip_order(self):
        self.assertEqual(network.list_interfaces(FakeRemote(answer(LISTING))), INTERFACES)

    def test_one_script_asks_for_links_addresses_and_drivers(self):
        remote = FakeRemote(answer(LISTING))
        network.list_interfaces(remote)
        self.assertEqual(len(remote.commands), 1)
        self.assertIn("ip -o link show", remote.commands[0])
        self.assertIn("ip -4 -o addr show", remote.commands[0])
        self.assertIn('"$d/device/driver"', remote.commands[0])

    def test_listing_changes_nothing(self):
        script = network.LIST_SCRIPT.replace("2>/dev/null", "")
        self.assertNotRegex(script, r"flush|addr add|link set|route|udhcpc|resolv|>")
        self.assertNotRegex(script, FORBIDDEN)

    def test_classification(self):
        cases = (
            ("wlan0", "ether", "", False, "wifi"),  # by name, for a driver that shows nothing in sysfs
            ("ap0", "ether", "sdio", True, "wifi"),  # by sysfs, for a name that shows nothing
            ("wlan1", "ether", "usb", True, "wifi"),  # a Wi-Fi stick
            ("usb0", "ether", "", False, "usb"),  # the camera's gadget has no bus to show
            ("usb1", "ether", "usb", False, "usb"),  # a phone's tether
            ("wwan0", "none", "usb", False, "usb"),
            ("eth0", "ether", "", False, "ethernet"),
            ("eth1", "ether", "usb", False, "ethernet"),  # an Ethernet adapter on a hub
            ("enx001122334455", "ether", "usb", False, "ethernet"),
            ("lan0", "ether", "platform", False, "ethernet"),
            ("dummy0", "ether", "", False, "other"),
            ("br0", "ether", "", False, "other"),
            ("sit0", "sit", "", False, "other"),
            ("tun0", "none", "", False, "other"),
        )
        for name, link, bus, wireless, kind in cases:
            with self.subTest(name=name, bus=bus):
                self.assertEqual(network._classify(name, link, bus, wireless), kind)

    def test_up_means_the_up_flag_itself(self):
        cases = (
            ("BROADCAST,MULTICAST,UP,LOWER_UP", True),
            ("NO-CARRIER,BROADCAST,MULTICAST,UP", True),
            ("UP", True),
            ("BROADCAST,MULTICAST", False),
            ("BROADCAST,LOWER_UP", False),  # the substring is not the flag
            ("", False),
        )
        for flags, up in cases:
            with self.subTest(flags=flags):
                text = f"4: eth0: <{flags}> mtu 1500 qdisc noop \\    link/ether 11:22:33:44:55:66\n"
                self.assertEqual(network.list_interfaces(FakeRemote(answer(text)))[0].up, up)

    def test_reads_iproute2_lines_too(self):
        text = (
            "2: eth0@if7: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc noqueue state UP mode DEFAULT group "
            "default qlen 1000\\    link/ether 11:22:33:44:55:66 brd 11:22:33:44:55:66 link-netnsid 0\\    altname e1\n"
            "2: eth0    inet 192.168.1.5/24 brd 192.168.1.255 scope global dynamic eth0\\       valid_lft 86313sec\n"
        )
        self.assertEqual(
            network.list_interfaces(FakeRemote(answer(text))),
            [NetworkInterface("eth0", "11:22:33:44:55:66", True, "ethernet", ["192.168.1.5/24"], "")],
        )

    def test_a_name_that_could_be_a_command_is_not_listed(self):
        text = (
            r"3: $(reboot): <UP> mtu 1500 qdisc noop \    link/ether 11:22:33:44:55:66" + "\n"
            r"4: eth0;x: <UP> mtu 1500 qdisc noop \    link/ether 11:22:33:44:55:66" + "\n"
            r"5: eth0: <UP> mtu 1500 qdisc noop \    link/ether 11:22:33:44:55:66" + "\n"
        )
        self.assertEqual([i.name for i in network.list_interfaces(FakeRemote(answer(text)))], ["eth0"])

    def test_control_characters_are_dropped_from_a_driver(self):
        text = r"5: eth0: <UP> mtu 1500 qdisc noop \    link/ether 11:22:33:44:55:66" + "\nnet=eth0\ndriver=r8\x1b[2J152\n"
        self.assertEqual(network.list_interfaces(FakeRemote(answer(text)))[0].driver, "r8[2J152")

    def test_a_folder_in_sysfs_that_ip_does_not_list_is_ignored(self):
        text = IP_LINK + "net=ghost0\ndriver=x\n"
        self.assertNotIn("ghost0", [i.name for i in network.list_interfaces(FakeRemote(answer(text)))])

    def test_a_failing_ip_raises_instead_of_looking_like_no_interfaces(self):
        remote = FakeRemote(answer(exit_code=127, stderr=b"sh: ip: not found"))
        with self.assertRaisesRegex(LabError, "ip: not found"):
            network.list_interfaces(remote)

    def test_a_lost_link_stays_a_network_error(self):
        with self.assertRaises(NetworkError):
            network.list_interfaces(FakeRemote(NetworkError("gone")))


class DhcpTests(unittest.TestCase):
    def test_exact_steps(self):
        remote = FakeRemote(answer(LISTING), readback("192.168.1.50/24"))
        network.configure(remote, "eth0")
        self.assertEqual(remote.commands[0], network.LIST_SCRIPT)
        self.assertEqual(
            argvs(remote.commands[1]),
            [
                ["ip", "link", "set", "dev", "eth0", "up"],
                ["udhcpc", "-i", "eth0", "-q", "-n"],
                ["ip", "-4", "-o", "addr", "show", "dev", "eth0"],
            ],
        )

    def test_returns_what_the_camera_shows(self):
        remote = FakeRemote(answer(LISTING), readback("192.168.1.50/24"))
        self.assertEqual(
            network.configure(remote, "eth0"),
            {"interface": "eth0", "mode": "dhcp", "addresses": ["192.168.1.50/24"], "gateway": None, "dns": None},
        )

    def test_never_flushes_or_touches_routes_or_resolv_conf(self):
        remote = FakeRemote(answer(LISTING), readback("192.168.1.50/24"))
        network.configure(remote, "eth0")
        self.assertNotRegex(remote.commands[1], r"flush|route|resolv")

    def test_ignores_udhcpc_chatter_and_other_interfaces_in_the_read_back(self):
        text = "udhcpc: lease of 192.168.1.50 obtained\n4: eth0    inet 192.168.1.50/24 scope global eth0\n"
        text += "2: wlan0    inet 10.5.5.9/24 scope global wlan0\n"
        result = network.configure(FakeRemote(answer(LISTING), answer(text)), "eth0")
        self.assertEqual(result["addresses"], ["192.168.1.50/24"])

    def test_dns_is_written_after_the_lease(self):
        remote = FakeRemote(answer(LISTING), readback("192.168.1.50/24"))
        result = network.configure(remote, "eth0", dns="192.168.1.1")
        self.assertEqual(
            argvs(remote.commands[1])[2],
            ["printf", "nameserver %s\\n", "192.168.1.1", ">", "/etc/resolv.conf"],
        )
        self.assertEqual(result["dns"], ["192.168.1.1"])

    def test_no_lease_raises(self):
        remote = FakeRemote(answer(LISTING), answer(exit_code=1, stderr=b"no lease on eth0"))
        with self.assertRaisesRegex(LabError, "no lease on eth0"):
            network.configure(remote, "eth0")

    def test_a_lease_that_left_no_address_raises(self):
        with self.assertRaisesRegex(LabError, "eth0 has no address"):
            network.configure(FakeRemote(answer(LISTING), answer("udhcpc: lease obtained\n")), "eth0")

    def test_a_lost_link_stays_a_network_error(self):
        with self.assertRaises(NetworkError):
            network.configure(FakeRemote(answer(LISTING), NetworkError("gone")), "eth0")


class StaticTests(unittest.TestCase):
    def test_exact_steps_in_order(self):
        remote = FakeRemote(answer(LISTING), readback("192.168.1.5/24"))
        network.configure(
            remote, "eth0", dhcp=False, address="192.168.1.5", gateway="192.168.1.1", dns=["1.1.1.1", "2606:4700::1111"]
        )
        self.assertEqual(
            argvs(remote.commands[1]),
            [
                ["ip", "link", "set", "dev", "eth0", "up"],
                ["ip", "addr", "flush", "dev", "eth0"],
                ["ip", "addr", "add", "192.168.1.5/24", "dev", "eth0"],
                ["ip", "route", "add", "default", "via", "192.168.1.1", "dev", "eth0"],
                ["printf", "nameserver %s\\n", "1.1.1.1", "2606:4700::1111", ">", "/etc/resolv.conf"],
                ["ip", "-4", "-o", "addr", "show", "dev", "eth0"],
            ],
        )

    def test_address_alone_sets_no_route_and_no_dns(self):
        remote = FakeRemote(answer(LISTING), readback("10.0.0.2/16"))
        result = network.configure(remote, "eth0", dhcp=False, address="10.0.0.2", prefix=16)
        self.assertEqual(
            argvs(remote.commands[1]),
            [
                ["ip", "link", "set", "dev", "eth0", "up"],
                ["ip", "addr", "flush", "dev", "eth0"],
                ["ip", "addr", "add", "10.0.0.2/16", "dev", "eth0"],
                ["ip", "-4", "-o", "addr", "show", "dev", "eth0"],
            ],
        )
        self.assertEqual(
            result,
            {"interface": "eth0", "mode": "static", "addresses": ["10.0.0.2/16"], "gateway": None, "dns": None},
        )

    def test_returns_the_gateway_and_dns_it_set(self):
        remote = FakeRemote(answer(LISTING), readback("192.168.1.5/24"))
        result = network.configure(remote, "eth0", dhcp=False, address="192.168.1.5", gateway="192.168.1.1", dns="1.1.1.1")
        self.assertEqual(result["gateway"], "192.168.1.1")
        self.assertEqual(result["dns"], ["1.1.1.1"])

    def test_an_address_that_did_not_stick_raises(self):
        remote = FakeRemote(answer(LISTING), readback("192.168.1.99/24"))
        with self.assertRaisesRegex(LabError, "192.168.1.5/24"):
            network.configure(remote, "eth0", dhcp=False, address="192.168.1.5")

    def test_no_forbidden_command(self):
        remote = FakeRemote(answer(LISTING), readback("192.168.1.5/24"))
        network.configure(remote, "eth0", dhcp=False, address="192.168.1.5", gateway="192.168.1.1", dns="1.1.1.1")
        self.assertNotRegex(remote.commands[1], FORBIDDEN)


class RefusalTests(unittest.TestCase):
    def assert_refused_before_anything_is_sent(self, *args, pattern=".", **kwargs):
        remote = FakeRemote()
        with self.assertRaisesRegex(LabError, pattern):
            network.configure(remote, *args, **kwargs)
        self.assertEqual(remote.commands, [])

    def test_bad_names(self):
        for name in BAD_NAMES:
            with self.subTest(name=name):
                self.assert_refused_before_anything_is_sent(name)
                self.assert_refused_before_anything_is_sent(name, dhcp=False, address="192.168.1.5")

    def test_loopback_by_name(self):
        self.assert_refused_before_anything_is_sent("lo", pattern="loopback")
        self.assert_refused_before_anything_is_sent("lo", dhcp=False, address="192.168.1.5", pattern="loopback")

    def test_loopback_under_another_name_is_caught_by_its_link_type(self):
        for line in (
            r"1: loop0: <UP> mtu 65536 qdisc noqueue \    link/loopback 11:22:33:44:55:66",
            r"1: loop0: <LOOPBACK,UP> mtu 65536 qdisc noqueue",
        ):
            with self.subTest(line=line):
                remote = FakeRemote(answer(line + "\n"))
                with self.assertRaisesRegex(LabError, "loopback"):
                    network.configure(remote, "loop0")
                self.assertEqual(remote.commands, [network.LIST_SCRIPT])

    def test_an_unknown_interface_gets_only_the_listing(self):
        remote = FakeRemote(answer(LISTING))
        with self.assertRaisesRegex(LabError, "no such interface"):
            network.configure(remote, "eth9", dhcp=False, address="192.168.1.5")
        self.assertEqual(remote.commands, [network.LIST_SCRIPT])

    def test_a_failed_listing_changes_nothing(self):
        remote = FakeRemote(answer(exit_code=127, stderr=b"ip: not found"))
        with self.assertRaisesRegex(LabError, "ip: not found"):
            network.configure(remote, "eth0")
        self.assertEqual(remote.commands, [network.LIST_SCRIPT])

    def test_bad_static_addresses(self):
        for address in (
            "",
            "banana",
            "300.1.1.1",
            "192.168.1",
            "192.168.1.5/24",
            "192.168.1.5; reboot",
            "$(reboot)",
            "192.168.1.5\nreboot",
            "::1",
            "0.0.0.0",
            "127.0.0.1",
            "224.0.0.1",
        ):
            with self.subTest(address=address):
                self.assert_refused_before_anything_is_sent("eth0", dhcp=False, address=address)

    def test_bad_prefixes(self):
        for prefix in (0, 33, -1, "24; reboot", "$(reboot)", "", None, True):
            with self.subTest(prefix=prefix):
                self.assert_refused_before_anything_is_sent("eth0", dhcp=False, address="192.168.1.5", prefix=prefix)

    def test_bad_gateways(self):
        for gateway in (
            "",
            "banana",
            "192.168.1.1; reboot",
            "$(reboot)",
            "fe80::1",
            "192.168.2.1",  # ip would only refuse this one after the flush
            "192.168.1.5",  # the interface's own address
        ):
            with self.subTest(gateway=gateway):
                self.assert_refused_before_anything_is_sent("eth0", dhcp=False, address="192.168.1.5", gateway=gateway)

    def test_bad_dns(self):
        for dns in ("", [], (), "banana", ["1.1.1.1", "banana"], "1.1.1.1; reboot", ["$(reboot)"], "1.1.1.1\nnameserver 6"):
            with self.subTest(dns=dns):
                self.assert_refused_before_anything_is_sent("eth0", dns=dns)
                self.assert_refused_before_anything_is_sent("eth0", dhcp=False, address="192.168.1.5", dns=dns)

    def test_dhcp_with_static_settings_is_refused_not_ignored(self):
        self.assert_refused_before_anything_is_sent("eth0", address="192.168.1.5", pattern="dhcp=False")
        self.assert_refused_before_anything_is_sent("eth0", gateway="192.168.1.1", pattern="dhcp=False")

    def test_static_without_an_address(self):
        self.assert_refused_before_anything_is_sent("eth0", dhcp=False, pattern="needs an address")
        self.assert_refused_before_anything_is_sent("eth0", dhcp=False, gateway="192.168.1.1", pattern="needs an address")


class QuotingTests(unittest.TestCase):
    def test_names_stay_one_word_even_past_validation(self):
        hostile = "$(reboot);x'y"
        listing = f"9: {hostile}: <UP> mtu 1500 qdisc noop \\    link/ether 11:22:33:44:55:66\n"
        remote = FakeRemote(answer(listing), answer(f"9: {hostile}    inet 192.168.1.5/24 scope global x\n"))
        with mock.patch.object(network, "_NAME", re.compile(r"\S+")):
            network.configure(remote, hostile, dhcp=False, address="192.168.1.5", gateway="192.168.1.1", dns="1.1.1.1")
        self.assertEqual(
            argvs(remote.commands[1]),
            [
                ["ip", "link", "set", "dev", hostile, "up"],
                ["ip", "addr", "flush", "dev", hostile],
                ["ip", "addr", "add", "192.168.1.5/24", "dev", hostile],
                ["ip", "route", "add", "default", "via", "192.168.1.1", "dev", hostile],
                ["printf", "nameserver %s\\n", "1.1.1.1", ">", "/etc/resolv.conf"],
                ["ip", "-4", "-o", "addr", "show", "dev", hostile],
            ],
        )

    def test_nothing_runs_on_this_computer(self):
        source = inspect.getsource(network)
        self.assertNotIn("subprocess", source)
        self.assertNotIn("shell=True", source)
        self.assertNotRegex(source, r"\bos\.system\b")


class LocalShell:
    """A ``Remote`` that is a shell on this computer, with temporary paths where the camera's are."""

    STUBS = r"""
ip() {
  echo "ip $*" >> "$ROOT/calls"
  case "$*" in
    "$IP_FAILS"*) echo "RTNETLINK answers: Operation not permitted" >&2; return 2 ;;
    "-o link show") cat "$ROOT/links" ;;
    "-4 -o addr show"*) cat "$ROOT/addrs" ;;
    "addr flush "*) : > "$ROOT/addrs" ;;
    "addr add "*) echo "4: $5    inet $3 scope global $5" >> "$ROOT/addrs" ;;
  esac
}
udhcpc() {
  echo "udhcpc $*" >> "$ROOT/calls"
  if [ -n "$DHCP_FAILS" ]; then echo "udhcpc: no lease, failing" >&2; return 1; fi
  echo "4: $2    inet 192.168.1.50/24 scope global $2" >> "$ROOT/addrs"
  echo "udhcpc: lease of 192.168.1.50 obtained"
}
"""

    def __init__(self, shell: list[str], root: Path) -> None:
        self.shell = shell
        self.root = root
        self.ip_fails = "no call starts like this"
        self.dhcp_fails = ""

    def exec(self, command: str, input: bytes | None = None, timeout: float = 120.0) -> ExecResult:
        command = command.replace("/sys/class/net", f"{self.root}/net")
        command = command.replace("/etc/resolv.conf", f"{self.root}/resolv.conf")
        variables = (
            f"ROOT={shlex.quote(str(self.root))}\nIP_FAILS={shlex.quote(self.ip_fails)}\n"
            f"DHCP_FAILS={shlex.quote(self.dhcp_fails)}\n"
        )
        done = subprocess.run(
            [*self.shell, "-c", variables + self.STUBS + command],
            cwd=self.root,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return ExecResult(exit_code=done.returncode, stdout=done.stdout, stderr=done.stderr)

    def read_file(self, path: str) -> bytes:
        raise AssertionError(f"network runs commands, but read {path}")

    def write_file(self, path: str, data: bytes) -> None:
        raise AssertionError(f"network runs commands, but wrote {path}")


class RealShellRuns:
    """The real scripts in a real shell. Subclasses say which shell."""

    shell: list[str]

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "net").mkdir()
        (self.root / "links").write_text(IP_LINK, encoding="utf-8")
        (self.root / "addrs").write_text(IP_ADDR, encoding="utf-8")
        self.resolv = self.root / "resolv.conf"
        self.resolv.write_text("nameserver 9.9.9.9\n", encoding="utf-8")
        self.remote = LocalShell(self.shell, self.root)

    def called(self) -> list[str]:
        """What ran after the listing, which configure always sends first."""
        calls = (self.root / "calls").read_text(encoding="utf-8").splitlines()
        return calls[calls.index("ip -4 -o addr show") + 1 :]

    def device(self, name: str, *, driver: str = "", bus: str = "", wireless: bool = False) -> None:
        folder = self.root / "net" / name
        folder.mkdir()
        if driver or bus:
            (folder / "device").mkdir()
        if driver:  # dangling, which readlink doesn't mind, and in the shape sysfs has
            (folder / "device" / "driver").symlink_to(f"../../../../bus/{bus}/drivers/{driver}")
        if bus:
            (folder / "device" / "subsystem").symlink_to(f"../../../../bus/{bus}")
        if wireless:
            (folder / "wireless").mkdir()

    def test_list_reads_a_sysfs_tree(self):
        self.device("lo")
        self.device("wlan0", driver="ath6kl_sdio", bus="sdio", wireless=True)
        self.device("usb0")
        self.device("eth0", driver="r8152", bus="usb")
        self.device("wlan1", driver="rtl8xxxu", bus="usb", wireless=True)
        self.device("wwan0", driver="qmi_wwan", bus="usb")
        self.device("lan0", driver="stmmaceth", bus="platform")
        self.device("dummy0")
        self.device("sit0")
        (self.root / "net" / "bonding_masters").write_text("\n", encoding="utf-8")  # a file sysfs really keeps here
        self.assertEqual(network.list_interfaces(self.remote), INTERFACES)

    def test_list_stops_when_ip_fails(self):
        self.remote.ip_fails = "-o link show"
        with self.assertRaisesRegex(LabError, "Operation not permitted"):
            network.list_interfaces(self.remote)

    def test_dhcp(self):
        result = network.configure(self.remote, "eth0")
        self.assertEqual(
            self.called(),
            ["ip link set dev eth0 up", "udhcpc -i eth0 -q -n", "ip -4 -o addr show dev eth0"],
        )
        self.assertIn("192.168.1.50/24", result["addresses"])
        self.assertEqual(self.resolv.read_text(encoding="utf-8"), "nameserver 9.9.9.9\n")

    def test_no_lease_stops_before_dns(self):
        self.remote.dhcp_fails = "1"
        with self.assertRaisesRegex(LabError, "no DHCP lease on eth0"):
            network.configure(self.remote, "eth0", dns="1.1.1.1")
        self.assertEqual(self.called(), ["ip link set dev eth0 up", "udhcpc -i eth0 -q -n"])
        self.assertEqual(self.resolv.read_text(encoding="utf-8"), "nameserver 9.9.9.9\n")

    def test_static(self):
        result = network.configure(
            self.remote, "usb0", dhcp=False, address="192.168.1.5", gateway="192.168.1.1", dns=["1.1.1.1", "8.8.8.8"]
        )
        self.assertEqual(
            self.called(),
            [
                "ip link set dev usb0 up",
                "ip addr flush dev usb0",
                "ip addr add 192.168.1.5/24 dev usb0",
                "ip route add default via 192.168.1.1 dev usb0",
                "ip -4 -o addr show dev usb0",
            ],
        )
        self.assertEqual(self.resolv.read_text(encoding="utf-8"), "nameserver 1.1.1.1\nnameserver 8.8.8.8\n")
        self.assertEqual(
            result,
            {
                "interface": "usb0",
                "mode": "static",
                "addresses": ["192.168.1.5/24"],  # the flush took the two it had
                "gateway": "192.168.1.1",
                "dns": ["1.1.1.1", "8.8.8.8"],
            },
        )

    def test_static_without_dns_leaves_resolv_conf_alone(self):
        network.configure(self.remote, "eth0", dhcp=False, address="192.168.1.5")
        self.assertEqual(self.resolv.read_text(encoding="utf-8"), "nameserver 9.9.9.9\n")

    def test_stops_at_the_first_failure_and_says_what_that_left(self):
        cases = (
            ("link set", "could not bring eth0 up", 1),
            ("addr flush", "could not clear the addresses on eth0", 2),
            ("addr add", "eth0 now has no address", 3),
            ("route add", "eth0 has its address, but no default route", 4),
        )
        for failing, message, steps in cases:
            with self.subTest(failing=failing):
                (self.root / "calls").unlink(missing_ok=True)
                self.remote.ip_fails = failing
                with self.assertRaisesRegex(LabError, message):
                    network.configure(
                        self.remote, "eth0", dhcp=False, address="192.168.1.5", gateway="192.168.1.1", dns="1.1.1.1"
                    )
                self.assertEqual(len(self.called()), steps)
                self.assertEqual(self.resolv.read_text(encoding="utf-8"), "nameserver 9.9.9.9\n")

    def test_resolv_conf_that_cannot_be_written_is_reported(self):
        self.resolv.unlink()
        self.resolv.mkdir()  # a folder can't be opened for writing, not even by root
        with self.assertRaisesRegex(LabError, "eth0 is set up, but .*resolv.conf could not be written"):
            network.configure(self.remote, "eth0", dns="1.1.1.1")
        self.assertNotIn("ip -4 -o addr show dev eth0", self.called())


@unittest.skipUnless(shutil.which("sh"), "no sh on this computer")
class PosixShellTests(RealShellRuns, unittest.TestCase):
    shell = ["sh"]


@unittest.skipUnless(shutil.which("busybox"), "no busybox on this computer")
class BusyBoxShellTests(RealShellRuns, unittest.TestCase):
    shell = ["busybox", "sh"]


if __name__ == "__main__":
    unittest.main()
