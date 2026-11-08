"""Resolving an SSH profile to an endpoint: USB-NCM discovery and transport precedence.

No hardware, no root, no real network. The camera's own CDC-NCM interface is faked as a
small ``/sys/class/net`` tree (an interface whose ``device`` symlink climbs to a USB device
with ``idVendor`` 2672), the address lookup and the TCP reachability probe are injected, and
``resolve()`` is driven through every transport including an explicit host that is down and
has to fall through to USB and then Wi-Fi.

Also what ``system_ssh`` runs for a password login. ``subprocess.run`` is faked there, so no ssh
ever starts; the one thing really executed is the askpass helper, a small shell script.
"""

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from quak.core.errors import NetworkError
from quak.ssh import system_ssh, transport
from quak.ssh.models import Endpoint, SshProfile
from quak.ssh.transport import (
    AP_HOST,
    UsbNet,
    camera_ip_from_host,
    camera_ip_from_serial,
    find_usb_nets,
    resolve,
)

# A GoPro serial long enough to carry the embedded octets. serial[11]='5', serial[12:14]='18'
# -> 172.2 5 . 1 18 . 51 -> 172.25.118.51 (the camera's own USB-NCM address).
GOPRO_SERIAL = "SERIAL00000518"
SERIAL_CAMERA_IP = "172.25.118.51"

ADDR = "AA:BB:CC:DD:EE:FF"


def _make_iface(net_dir: Path, name: str, *, vid: str | None, pid: str = "0052") -> None:
    """Create a fake ``/sys/class/net/<name>`` whose ``device`` points at a USB device.

    When ``vid`` is given the walk from the interface's ``device`` reaches a USB device dir
    carrying ``idVendor``/``idProduct`` (what ``find_usb_nets`` keys on). When ``vid`` is
    None the interface has a ``device`` with no USB ids above it (e.g. a virtual NIC).
    """
    usb_device = net_dir.parent / "devices" / name / "usb_device"
    interface = usb_device / "interface"
    interface.mkdir(parents=True)
    if vid is not None:
        (usb_device / "idVendor").write_text(vid + "\n")
        (usb_device / "idProduct").write_text(pid + "\n")

    iface = net_dir / name
    iface.mkdir(parents=True)
    # Real sysfs exposes net/<name>/device as a symlink into the USB interface node; the
    # resolver climbs its parents looking for idVendor. Mirror that with a relative symlink.
    (iface / "device").symlink_to(Path("..") / ".." / "devices" / name / "usb_device" / "interface")


class DerivationTests(unittest.TestCase):
    def test_serial_to_camera_ip(self):
        self.assertEqual(camera_ip_from_serial(GOPRO_SERIAL), SERIAL_CAMERA_IP)

    def test_serial_tolerates_surrounding_whitespace(self):
        self.assertEqual(camera_ip_from_serial(f"  {GOPRO_SERIAL}\n"), SERIAL_CAMERA_IP)

    def test_serial_none_or_short_is_none(self):
        self.assertIsNone(camera_ip_from_serial(None))
        self.assertIsNone(camera_ip_from_serial(""))
        self.assertIsNone(camera_ip_from_serial("C34613"))

    def test_serial_with_non_digit_octets_is_none(self):
        # serial[11:14] must be digits; an alpha there means no derivable address.
        self.assertIsNone(camera_ip_from_serial("C346132656AB18"))

    def test_host_to_camera_ip_replaces_last_octet_with_51(self):
        self.assertEqual(camera_ip_from_host("172.25.118.22"), "172.25.118.51")
        self.assertEqual(camera_ip_from_host("10.5.5.9"), "10.5.5.51")

    def test_host_not_ipv4_raises(self):
        with self.assertRaises(ValueError):
            camera_ip_from_host("not-an-ip")
        with self.assertRaises(ValueError):
            camera_ip_from_host("1.2.3")


class FindUsbNetsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.net_dir = Path(self.tmp.name) / "sys" / "class" / "net"
        self.net_dir.mkdir(parents=True)

    def test_finds_the_gopro_ncm_interface(self):
        _make_iface(self.net_dir, "ncm0", vid=transport.CAMERA_USB_VID)
        addresses = {"ncm0": "172.25.118.22"}
        nets = find_usb_nets(self.net_dir, ipv4=lambda name: addresses.get(name, ""))
        self.assertEqual(
            nets,
            [UsbNet(interface="ncm0", ipv4="172.25.118.22", vid=transport.CAMERA_USB_VID, pid="0052")],
        )

    def test_ignores_other_vendors(self):
        _make_iface(self.net_dir, "ncm0", vid=transport.CAMERA_USB_VID)
        _make_iface(self.net_dir, "enp0s20", vid="0bda")  # some USB Ethernet dongle
        nets = find_usb_nets(self.net_dir, ipv4=lambda name: "172.25.118.22")
        self.assertEqual([n.interface for n in nets], ["ncm0"])

    def test_skips_interfaces_without_a_device(self):
        # A purely virtual interface (no device symlink at all), like loopback.
        (self.net_dir / "lo").mkdir()
        _make_iface(self.net_dir, "ncm0", vid=transport.CAMERA_USB_VID)
        nets = find_usb_nets(self.net_dir, ipv4=lambda name: "172.25.118.22")
        self.assertEqual([n.interface for n in nets], ["ncm0"])

    def test_skips_gopro_interface_with_no_address(self):
        _make_iface(self.net_dir, "ncm0", vid=transport.CAMERA_USB_VID)
        nets = find_usb_nets(self.net_dir, ipv4=lambda name: "")
        self.assertEqual(nets, [])

    def test_missing_sys_tree_returns_empty(self):
        nets = find_usb_nets(self.net_dir / "nope", ipv4=lambda name: "172.25.118.22")
        self.assertEqual(nets, [])

    def test_default_ipv4_lookup_is_patchable(self):
        _make_iface(self.net_dir, "ncm0", vid=transport.CAMERA_USB_VID)
        calls = []

        def fake_ipv4(name):
            calls.append(name)
            return "172.25.118.22"

        # No ``ipv4=`` argument: find_usb_nets must fall back to the module helper, so patching
        # it keeps the discovery testable without the ``ip`` tool.
        original = transport._ipv4
        transport._ipv4 = fake_ipv4
        try:
            nets = find_usb_nets(self.net_dir)
        finally:
            transport._ipv4 = original
        self.assertEqual(calls, ["ncm0"])
        self.assertEqual(nets[0].ipv4, "172.25.118.22")


def _reach(*reachable):
    """A reachability probe that answers True only for the given (host, port) pairs."""
    allowed = set(reachable)

    def probe(host, port, timeout=2.0):
        return (host, port) in allowed

    return probe


def _nets(*nets):
    def scan():
        return list(nets)

    return scan


class ResolveTests(unittest.TestCase):
    def _profile(self, **kw):
        return SshProfile(address=ADDR, **kw)

    # host --------------------------------------------------------------------

    def test_host_transport_uses_stored_host_without_probing(self):
        profile = self._profile(transport="host", host="192.168.9.9")
        endpoint = resolve(
            profile,
            reachable=_reach(),  # nothing reachable; host transport must not care
            find_nets=_nets(),
        )
        self.assertEqual(endpoint.host, "192.168.9.9")
        self.assertEqual(endpoint.transport, "host")

    def test_host_transport_without_host_raises(self):
        profile = self._profile(transport="host")
        with self.assertRaises(NetworkError):
            resolve(profile, reachable=_reach(), find_nets=_nets())

    # usb ---------------------------------------------------------------------

    def test_usb_prefers_serial_derived_address(self):
        profile = self._profile(transport="usb")
        net = UsbNet(interface="ncm0", ipv4="172.25.118.22", vid=transport.CAMERA_USB_VID, pid="0052")
        endpoint = resolve(
            profile,
            serial=GOPRO_SERIAL,
            reachable=_reach((SERIAL_CAMERA_IP, 80)),
            find_nets=_nets(net),
        )
        self.assertEqual(endpoint.host, SERIAL_CAMERA_IP)
        self.assertEqual(endpoint.transport, "usb")
        self.assertEqual(endpoint.interface, "ncm0")

    def test_usb_falls_back_to_host_derived_address(self):
        # No serial, so the camera IP comes from the host's /24 with .51.
        profile = self._profile(transport="usb")
        net = UsbNet(interface="ncm0", ipv4="172.25.118.22", vid=transport.CAMERA_USB_VID, pid="0052")
        endpoint = resolve(
            profile,
            serial=None,
            reachable=_reach(("172.25.118.51", 80)),
            find_nets=_nets(net),
        )
        self.assertEqual(endpoint.host, "172.25.118.51")
        self.assertEqual(endpoint.transport, "usb")

    def test_usb_accepts_port_8080(self):
        profile = self._profile(transport="usb")
        net = UsbNet(interface="ncm0", ipv4="172.25.118.22", vid=transport.CAMERA_USB_VID, pid="0052")
        endpoint = resolve(
            profile,
            serial=GOPRO_SERIAL,
            reachable=_reach((SERIAL_CAMERA_IP, 8080)),
            find_nets=_nets(net),
        )
        self.assertEqual(endpoint.host, SERIAL_CAMERA_IP)

    def test_usb_with_no_interface_raises(self):
        profile = self._profile(transport="usb")
        with self.assertRaises(NetworkError):
            resolve(profile, serial=GOPRO_SERIAL, reachable=_reach(), find_nets=_nets())

    def test_usb_with_unreachable_camera_raises(self):
        profile = self._profile(transport="usb")
        net = UsbNet(interface="ncm0", ipv4="172.25.118.22", vid=transport.CAMERA_USB_VID, pid="0052")
        with self.assertRaises(NetworkError):
            resolve(profile, serial=GOPRO_SERIAL, reachable=_reach(), find_nets=_nets(net))

    # wifi --------------------------------------------------------------------

    def test_wifi_transport_returns_access_point(self):
        profile = self._profile(transport="wifi")
        endpoint = resolve(profile, reachable=_reach(), find_nets=_nets())
        self.assertEqual(endpoint.host, AP_HOST)
        self.assertEqual(endpoint.transport, "wifi")

    # auto precedence ---------------------------------------------------------

    def test_auto_prefers_reachable_explicit_host(self):
        profile = self._profile(transport="auto", host="192.168.9.9")
        net = UsbNet(interface="ncm0", ipv4="172.25.118.22", vid=transport.CAMERA_USB_VID, pid="0052")
        endpoint = resolve(
            profile,
            serial=GOPRO_SERIAL,
            reachable=_reach(("192.168.9.9", 22), (SERIAL_CAMERA_IP, 80)),
            find_nets=_nets(net),
        )
        self.assertEqual(endpoint.host, "192.168.9.9")
        self.assertEqual(endpoint.transport, "host")

    def test_auto_unreachable_host_falls_through_to_usb(self):
        profile = self._profile(transport="auto", host="192.168.9.9")
        net = UsbNet(interface="ncm0", ipv4="172.25.118.22", vid=transport.CAMERA_USB_VID, pid="0052")
        endpoint = resolve(
            profile,
            serial=GOPRO_SERIAL,
            # host down; camera's own USB-NCM answers
            reachable=_reach((SERIAL_CAMERA_IP, 80)),
            find_nets=_nets(net),
        )
        self.assertEqual(endpoint.host, SERIAL_CAMERA_IP)
        self.assertEqual(endpoint.transport, "usb")

    def test_auto_unreachable_host_and_no_usb_falls_through_to_wifi(self):
        profile = self._profile(transport="auto", host="192.168.9.9")
        endpoint = resolve(
            profile,
            serial=GOPRO_SERIAL,
            reachable=_reach(),  # host down, nothing else reachable
            find_nets=_nets(),  # no USB interface
        )
        self.assertEqual(endpoint.host, AP_HOST)
        self.assertEqual(endpoint.transport, "wifi")

    def test_auto_with_no_host_tries_usb_then_wifi(self):
        profile = self._profile(transport="auto")
        endpoint = resolve(profile, serial=GOPRO_SERIAL, reachable=_reach(), find_nets=_nets())
        self.assertEqual(endpoint.host, AP_HOST)
        self.assertEqual(endpoint.transport, "wifi")

    def test_auto_skips_host_probe_when_disabled(self):
        # probe_custom_host=False trusts the stored host without a TCP check.
        profile = self._profile(transport="auto", host="192.168.9.9")
        endpoint = resolve(profile, probe_custom_host=False, reachable=_reach(), find_nets=_nets())
        self.assertEqual(endpoint.host, "192.168.9.9")
        self.assertEqual(endpoint.transport, "host")


PASSWORD = "pa ss$(touch pwned)'\"`-n\\"
PASSWORD_HOST = "192.168.9.9"
PASSWORD_OPTIONS = (
    "BatchMode=no",
    "PreferredAuthentications=password,keyboard-interactive",
    "PubkeyAuthentication=no",
    "NumberOfPasswordPrompts=1",
)


class PasswordLoginTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.dict(os.environ, {"QUAK_CONFIG_DIR": tmp.name})
        patch.start()
        self.addCleanup(patch.stop)
        self.profile = SshProfile(address=ADDR, transport="host", host=PASSWORD_HOST, user="admin")
        self.endpoint = Endpoint(PASSWORD_HOST, 22, 80, "host")
        self.result = subprocess.CompletedProcess([], 0, b"uid=0(root)\nLinux 4.9 armv7l\n", b"")
        self.error = None
        self.runs = []
        real_run = subprocess.run

        def fake_run(args, **kwargs):
            run = SimpleNamespace(args=args, kwargs=kwargs, helper=None)
            self.runs.append(run)
            if "env" in kwargs:  # look at the helper now: it's gone once this call returns
                run.helper = Path(kwargs["env"]["SSH_ASKPASS"])
                run.mode = stat.S_IMODE(run.helper.stat().st_mode)
                run.script = run.helper.read_text()
                run.printed = real_run(
                    [str(run.helper)], env=kwargs["env"], cwd=tmp.name, capture_output=True, check=True
                ).stdout
            if self.error:
                raise self.error
            return self.result

        patch = mock.patch.object(system_ssh.subprocess, "run", side_effect=fake_run)
        patch.start()
        self.addCleanup(patch.stop)

    def calls(self) -> dict:
        profile, endpoint = self.profile, self.endpoint
        return {
            "test": lambda **kw: system_ssh.test(profile, endpoint, None, **kw),
            "exec_command": lambda **kw: system_ssh.exec_command(profile, endpoint, None, "uptime", **kw),
            "read_file": lambda **kw: system_ssh.read_file(profile, endpoint, None, "/tmp/a", **kw),
            "write_file": lambda **kw: system_ssh.write_file(profile, endpoint, None, "/tmp/a", b"data", **kw),
        }

    def test_every_call_keeps_the_password_out_of_argv_and_in_the_environment(self):
        for name, call in self.calls().items():
            with self.subTest(name):
                call(password=PASSWORD)
                run = self.runs[-1]
                self.assertFalse([arg for arg in run.args if PASSWORD in arg or "pa ss" in arg])
                env = run.kwargs["env"]
                self.assertEqual(env["QUAK_SSH_PASSWORD"], PASSWORD)
                self.assertEqual(env["SSH_ASKPASS"], str(run.helper))
                self.assertEqual(env["SSH_ASKPASS_REQUIRE"], "force")
                self.assertIs(run.kwargs["start_new_session"], True)

    def test_every_call_asks_ssh_for_a_password_login_only(self):
        for name, call in self.calls().items():
            with self.subTest(name):
                call(password=PASSWORD)
                args = self.runs[-1].args
                for option in PASSWORD_OPTIONS:
                    self.assertEqual(args[args.index(option) - 1], "-o")
                self.assertNotIn("BatchMode=yes", args)
                self.assertIn("StrictHostKeyChecking=yes", args)  # a password goes to the pinned camera or nowhere
                last_option = max(i for i, arg in enumerate(args) if arg == "-o")
                self.assertLess(last_option, args.index(f"admin@{PASSWORD_HOST}"))

    @unittest.skipUnless(os.name == "posix", "POSIX file modes and a shell script")
    def test_the_helper_is_private_prints_the_password_exactly_and_holds_none_of_it(self):
        system_ssh.test(self.profile, self.endpoint, None, password=PASSWORD)
        run = self.runs[-1]
        self.assertEqual(run.mode, 0o700)
        self.assertEqual(run.printed, PASSWORD.encode() + b"\n")
        self.assertNotIn(PASSWORD, run.script)

    def test_the_helper_is_removed_after_every_call(self):
        for name, call in self.calls().items():
            with self.subTest(name):
                call(password=PASSWORD)
                self.assertFalse(self.runs[-1].helper.exists())

    def test_the_helper_is_removed_and_the_password_kept_out_of_the_error_when_ssh_fails(self):
        for error in (subprocess.TimeoutExpired(["ssh"], 1), OSError("no ssh")):
            with self.subTest(type(error).__name__):
                self.error = error
                with self.assertRaises(NetworkError) as caught:
                    system_ssh.exec_command(self.profile, self.endpoint, None, "uptime", password=PASSWORD)
                self.assertNotIn(PASSWORD, str(caught.exception))
                self.assertFalse(self.runs[-1].helper.exists())

    def test_the_password_never_enters_this_process_environment(self):
        # Single values on purpose: a failed assertion against os.environ itself prints all of it, secrets included.
        system_ssh.test(self.profile, self.endpoint, None, password=PASSWORD)
        self.assertIsNone(os.environ.get("QUAK_SSH_PASSWORD"))

    def test_the_helper_wins_over_askpass_settings_already_in_the_environment(self):
        with mock.patch.dict(os.environ, {"SSH_ASKPASS": "/bin/false", "SSH_ASKPASS_REQUIRE": "never"}):
            system_ssh.test(self.profile, self.endpoint, None, password=PASSWORD)
            run = self.runs[-1]
            self.assertEqual(run.kwargs["env"]["SSH_ASKPASS"], str(run.helper))
            self.assertEqual(run.kwargs["env"]["SSH_ASKPASS_REQUIRE"], "force")
            self.assertEqual(os.environ.get("SSH_ASKPASS"), "/bin/false")  # only ssh's copy changes
            self.assertEqual(os.environ.get("SSH_ASKPASS_REQUIRE"), "never")

    def test_stdin_is_devnull_unless_it_has_data_to_carry(self):
        for name, call in self.calls().items():
            with self.subTest(name):
                call(password=PASSWORD)
                kwargs = self.runs[-1].kwargs
                if name == "write_file":
                    # subprocess refuses stdin= next to input=, and the file's bytes travel on stdin
                    self.assertEqual(kwargs["input"], b"data")
                    self.assertIsNone(kwargs["stdin"])
                else:
                    self.assertIsNone(kwargs["input"])
                    self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)

    def test_a_key_path_does_not_undo_a_password_login(self):
        system_ssh.test(self.profile, self.endpoint, Path("/keys/k.pem"), password=PASSWORD)
        args = self.runs[-1].args
        self.assertNotIn("-i", args)
        self.assertNotIn("PasswordAuthentication=no", args)

    def test_a_failed_password_login_comes_back_in_the_result(self):
        self.result = subprocess.CompletedProcess([], 255, b"", b"Permission denied, please try again.\n")
        result = system_ssh.test(self.profile, self.endpoint, None, password=PASSWORD)
        self.assertFalse(result.ok)
        self.assertIn("Permission denied", result.error)

    def test_a_key_login_runs_exactly_as_it_did(self):
        key = Path("/keys/k.pem")
        for name, call in {
            "test": lambda: system_ssh.test(self.profile, self.endpoint, key),
            "exec_command": lambda: system_ssh.exec_command(self.profile, self.endpoint, key, "uptime"),
            "read_file": lambda: system_ssh.read_file(self.profile, self.endpoint, key, "/tmp/a"),
            "write_file": lambda: system_ssh.write_file(self.profile, self.endpoint, key, "/tmp/a", b"data"),
        }.items():
            with self.subTest(name):
                call()
                run = self.runs[-1]
                self.assertEqual(run.args[run.args.index("-i") + 1], str(key))
                self.assertIn("PasswordAuthentication=no", run.args)
                self.assertIn("BatchMode=yes", run.args)
                self.assertFalse(set(PASSWORD_OPTIONS) & set(run.args))
                self.assertEqual(set(run.kwargs), {"input", "capture_output", "timeout"})


if __name__ == "__main__":
    unittest.main()
