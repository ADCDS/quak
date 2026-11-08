"""The ``quak lab`` command line.

No hardware, no real ``ssh``, no subprocess and no network: ``LabService`` is replaced with an autospec of itself, so a
call the real one wouldn't accept fails here, and everything on disk runs against an isolated ``QUAK_CONFIG_DIR``. The
CLI is driven in-process through ``main()`` so exit codes and ``--json`` output can be checked.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from quak import config
from quak.cli.main import build_parser, main
from quak.core.errors import HostModeError, LabError, NetworkError, ToolkitError
from quak.lab.capabilities.storage import CopyResult, DirEntry
from quak.lab.models import (
    HostModeResult,
    HostModeState,
    LabStatus,
    NetworkInterface,
    PackState,
    UsbDevice,
    UsbInterface,
    Volume,
)
from quak.lab.service import LabService
from quak.ssh import profiles
from quak.ssh.manager import SshManager
from quak.ssh.models import Endpoint, SshProfile
from quak.ssh.transport import UsbNet

ADDRESS = "AA:BB:CC:DD:EE:FF"
HOST = "192.168.1.50"
PACK_PATH = "/tmp/fuse_d/quak/toolkit/packs/core"
VENDOR_PATH = "/tmp/fuse_d/quak/toolkit/packs/vendor"
PACK_DIR = "/home/me/packs/vendor"
SIGNERS = "/home/me/allowed_signers"
MOUNT = "/tmp/quak/mnt/sda1"
SRC = f"{MOUNT}/clip.wav"
DST = "/tmp/fuse_d/clip.wav"
SHA = "ab" * 32
SECRET = "correct horse battery"

PORT_ON =HostModeState(enabled=True, supported=True, hostmode="1", devices=["usb1", "usb2"], marker="host")
PORT_OFF = HostModeState(supported=True, hostmode="2", udc="19880000.dwc3")
AUDIO = UsbDevice(
    id="1-1",
    vid="05ac",
    pid="110a",
    product="USB-C Adapter",
    manufacturer="Apple",
    bclass="00",
    speed="480",
    driver="usb",
    interfaces=[UsbInterface("1-1:1.0", "01", "snd-usb-audio")],
)

# Each action --yes guards: the command line, the service method it ends in, and what that method is given.
GUARDED = [
    (("host", "on"), "host_on", ()),
    (("host", "off"), "host_off", ()),
    (("toolkit", "install"), "toolkit_install", ()),
    (("toolkit", "remove"), "toolkit_remove", ()),
    (("storage", "mount", "sda1"), "storage_mount", ("sda1",)),
    (("storage", "mount", "sda1", "card"), "storage_mount", ("sda1", "card")),
    (("storage", "umount", MOUNT), "storage_unmount", (MOUNT,)),
    (("storage", "copy", SRC, DST), "storage_copy", (SRC, DST)),
    (("storage", "eject", MOUNT), "storage_eject", (MOUNT,)),
    (("network", "configure", "eth0"), "network_configure", ("eth0", True)),
]

UNGUARDED = [
    (("status",), "status", ()),
    (("host", "recover"), "host_recover", ()),
    (("devices",), "devices", ()),
    (("toolkit", "list"), "toolkit_status", ()),
    (("toolkit", "load"), "toolkit_load", ()),
    (("toolkit", "unload"), "toolkit_unload", ()),
    (("storage", "list"), "storage_list", ()),
    (("storage", "list", MOUNT), "storage_list_dir", (MOUNT,)),
    (("network", "list"), "network_list", ()),
    (("system",), "system_status", ()),
    (("system", "status"), "system_status", ()),
    (("system", "logs"), "system_logs", ()),
    (("system", "processes"), "system_processes", ()),
]


class LabCliCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"QUAK_CONFIG_DIR": tmp.name, "QUAK_SIM": "0"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("QUAK_CAMERA_ADDRESS", None)  # patch.dict puts it back

        self.lab = mock.create_autospec(LabService, instance=True)
        self.lab.status.return_value = LabStatus(host_mode=PORT_OFF, toolkit=PackState(installed=True, path=PACK_PATH))
        self.lab.host_on.return_value = HostModeResult(PORT_ON, ["paused gpUsb", "hostmode=1"], "Host mode is on")
        self.lab.host_off.return_value = HostModeResult(PORT_OFF, ["hostmode=2"], "Restored: hostmode=2")
        self.lab.host_recover.return_value = HostModeResult(PORT_OFF, [], "Nothing to recover")
        self.lab.devices.return_value = [AUDIO]
        self.lab.toolkit_status.return_value = PackState(installed=True, path=PACK_PATH)
        self.lab.toolkit_install.return_value = PackState(installed=True, path=PACK_PATH)
        self.lab.toolkit_install_pack.return_value = PackState(installed=True, path=VENDOR_PATH)
        self.lab.toolkit_load.return_value = PackState(installed=True, loaded=True, path=PACK_PATH)
        self.lab.toolkit_unload.return_value = PackState(installed=True, path=PACK_PATH)
        self.lab.toolkit_remove.return_value = PackState()
        self.lab.storage_list.return_value = [Volume("/dev/sda1", label="CARD", fs="vfat", size=32_000_000_000)]
        self.lab.storage_list_dir.return_value = [DirEntry("clip.wav", "file", 2_000_000), DirEntry("old", "dir", 0)]
        self.lab.storage_mount.return_value = Volume("/dev/sda1", fs="vfat", mountpoint=MOUNT, mounted=True)
        self.lab.storage_unmount.return_value = None
        self.lab.storage_eject.return_value = None
        self.lab.storage_copy.return_value = CopyResult(DST, SHA)
        self.lab.network_list.return_value = [
            NetworkInterface("lo", up=True, addresses=["127.0.0.1/8"]),
            NetworkInterface(
                "eth0", mac="11:22:33:44:55:66", up=True, kind="ethernet", addresses=["10.0.0.2/24"], driver="r8152"
            ),
            NetworkInterface("wlan0", mac="AA:BB:CC:DD:EE:FF", kind="wifi"),
        ]
        self.lab.network_configure.return_value = {
            "interface": "eth0",
            "mode": "dhcp",
            "addresses": ["10.0.0.7/24"],
            "gateway": None,
            "dns": None,
        }
        self.lab.system_status.return_value = {
            "uptime": 3725.5,
            "load": [0.5, 0.25, 0.1],
            "mem": {"total": 1_000_000_000, "free": 200_000_000, "available": 500_000_000},
            "temp": [{"zone": "thermal_zone0", "type": "cpu", "celsius": 42.5}],
            "df": {"path": "/tmp/fuse_d", "total": 32_000_000_000, "used": 2_000_000_000, "free": 30_000_000_000,
                   "percent": 7},
        }
        self.lab.system_logs.return_value = "usb 1-1: new device\n"
        self.lab.system_processes.return_value = "PID USER COMMAND\n"

        self.service = self._patch("quak.cli.main.LabService", return_value=self.lab)
        self.recover_network = self._patch("quak.cli.main.recover_network", return_value=[])
        self.ble_run = self._patch("quak.cli.main.run")
        self._patch("subprocess.Popen", side_effect=AssertionError("a test started a real subprocess"))
        self._patch("socket.create_connection", side_effect=AssertionError("a test opened a real connection"))

    def _patch(self, target, **kwargs):
        patcher = mock.patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def run_cli(self, *argv, json_mode=True, address=ADDRESS):
        """Run ``quak [--json] lab ...`` in-process and return (exit_code, parsed-json, stdout, stderr)."""
        prefix = ["--json"] if json_mode else []
        suffix = ["--address", address] if address else []
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                main([*prefix, "lab", *argv, *suffix])
            except SystemExit as exc:
                code = exc.code or 0
        text = out.getvalue()
        return code, json.loads(text) if json_mode and text.strip() else {}, text, err.getvalue()

    def reset(self):
        self.lab.reset_mock()
        self.service.reset_mock()


class ParserTests(LabCliCase):
    def test_every_action_parses_with_its_args(self):
        parse = build_parser().parse_args
        for argv, _, _ in [*GUARDED, *UNGUARDED]:
            with self.subTest(argv=argv):
                a = parse(["lab", *argv])
                self.assertEqual((a.lab_action, a.args), (argv[0], list(argv[1:])))

    def test_options_are_accepted_after_the_action(self):
        a = build_parser().parse_args(
            ["lab", "host", "on", "--address", ADDRESS, "--host", HOST, "--transport", "wifi", "--json", "--yes"]
        )
        self.assertEqual((a.address, a.host, a.transport, a.json, a.yes), (ADDRESS, HOST, "wifi", True, True))

    def test_options_are_accepted_before_the_action(self):
        a = build_parser().parse_args(["--json", "--address", ADDRESS, "lab", "--yes", "host", "on"])
        self.assertEqual((a.address, a.json, a.yes), (ADDRESS, True, True))
        self.assertEqual((a.lab_action, a.args), ("host", ["on"]))

    def test_yes_is_off_unless_given(self):
        self.assertFalse(build_parser().parse_args(["lab", "host", "on"]).yes)

    def test_an_unknown_action_or_transport_is_refused_by_the_parser(self):
        for argv in (("format",), ("status", "--transport", "carrier-pigeon")):
            with self.subTest(argv=argv):
                code, _, _, err = self.run_cli(*argv, json_mode=False)
                self.assertEqual(code, 2)
                self.assertIn("invalid choice", err)
        self.service.assert_not_called()


class DispatchTests(LabCliCase):
    def test_lab_takes_neither_the_ble_path_nor_the_network_recovery(self):
        for argv, _, _ in UNGUARDED:
            with self.subTest(argv=argv):
                code, _, _, _ = self.run_cli(*argv)
                self.assertEqual(code, 0)
        self.ble_run.assert_not_called()
        self.recover_network.assert_not_called()

    def test_a_refused_lab_action_skips_them_too(self):
        code, _, _, _ = self.run_cli("host", "on")
        self.assertEqual(code, 12)
        self.ble_run.assert_not_called()
        self.recover_network.assert_not_called()

    def test_other_commands_still_get_both(self):
        main(["info", "--address", ADDRESS])
        self.ble_run.assert_called_once()
        self.recover_network.assert_called_once()
        self.service.assert_not_called()

    def test_the_service_gets_a_manager_the_address_and_its_profile(self):
        self.run_cli("status")
        args, kwargs = self.service.call_args
        self.assertIsInstance(args[0], SshManager)
        self.assertEqual(args[1], ADDRESS)
        self.assertEqual(kwargs["profile"].address, ADDRESS)
        self.assertEqual(kwargs["profile"].transport, "auto")

    def test_host_and_transport_override_the_saved_profile_for_this_run_only(self):
        profiles.save(SshProfile(address=ADDRESS, label="cam", transport="wifi"))
        self.run_cli("status", "--host", HOST, "--transport", "host")
        profile = self.service.call_args.kwargs["profile"]
        self.assertEqual((profile.label, profile.host, profile.transport), ("cam", HOST, "host"))
        saved = profiles.load(ADDRESS)
        self.assertEqual((saved.host, saved.transport), (None, "wifi"))

    def test_the_paired_camera_is_the_default(self):
        config.remember_camera(ADDRESS, model="HERO11 Black")
        code, _, _, _ = self.run_cli("status", address=None)
        self.assertEqual(code, 0)
        self.assertEqual(self.service.call_args.args[1], ADDRESS)

    def test_no_camera_at_all_is_an_ssh_error(self):
        code, data, _, _ = self.run_cli("status", address=None)
        self.assertEqual(code, 8)
        self.assertIn("--address", data["error"])
        self.service.assert_not_called()

    def test_sim_is_refused_rather_than_reaching_a_real_camera(self):
        code, data, _, _ = self.run_cli("host", "on", "--yes", "--sim")
        self.assertEqual(code, 1)
        self.assertIn("real hardware", data["error"])
        self.service.assert_not_called()


class ConfirmationTests(LabCliCase):
    def test_without_yes_a_guarded_action_never_builds_a_service(self):
        for argv, _, _ in GUARDED:
            with self.subTest(argv=argv):
                code, data, _, _ = self.run_cli(*argv)
                self.assertEqual(code, 12)
                self.assertIn("--yes", data["error"])
        self.service.assert_not_called()
        self.assertEqual(self.lab.method_calls, [])

    def test_with_yes_a_guarded_action_runs_that_one_method(self):
        for argv, method, args in GUARDED:
            with self.subTest(argv=argv):
                self.reset()
                code, data, _, _ = self.run_cli(*argv, "--yes")
                self.assertEqual(code, 0, data)
                getattr(self.lab, method).assert_awaited_once_with(*args)
                self.assertEqual(len(self.lab.method_calls), 1)

    def test_the_rest_need_no_yes_and_run_that_one_method(self):
        for argv, method, args in UNGUARDED:
            with self.subTest(argv=argv):
                self.reset()
                code, data, _, _ = self.run_cli(*argv)
                self.assertEqual(code, 0, data)
                getattr(self.lab, method).assert_awaited_once_with(*args)
                self.assertEqual(len(self.lab.method_calls), 1)

    def test_a_mount_leaves_read_only_to_the_default_and_the_cli_cannot_ask_for_write(self):
        self.run_cli("storage", "mount", "sda1", "--yes")
        self.assertNotIn("read_only", self.lab.storage_mount.await_args.kwargs)
        code, _, _, err = self.run_cli("storage", "mount", "sda1", "--yes", "--rw", json_mode=False)
        self.assertEqual(code, 2)
        self.assertIn("--rw", err)


class UsageTests(LabCliCase):
    def test_a_wrong_subcommand_or_argument_count_shows_usage_and_does_nothing(self):
        cases = [
            (("status", "extra"), "quak lab status"),
            (("devices", "extra"), "quak lab devices"),
            (("host",), "on|off|recover"),
            (("host", "sideways"), "on|off|recover"),
            (("host", "on", "extra"), "on|off|recover"),
            (("toolkit",), "list|install|load|unload|remove"),
            (("toolkit", "format"), "list|install|load|unload|remove"),
            (("toolkit", "load", "core"), "list|install|load|unload|remove"),
            (("storage",), "mount DEVICE"),
            (("storage", "format", "sda1"), "mount DEVICE"),
            (("storage", "list", "a", "b"), "mount DEVICE"),
            (("storage", "mount"), "mount DEVICE"),
            (("storage", "mount", "a", "b", "c"), "mount DEVICE"),
            (("storage", "umount"), "umount MOUNTPOINT"),
            (("storage", "umount", "a", "b"), "umount MOUNTPOINT"),
            (("storage", "copy", SRC), "copy SRC DST"),
            (("storage", "copy", SRC, DST, "extra"), "copy SRC DST"),
            (("storage", "eject"), "eject MOUNTPOINT"),
            (("network",), "configure NAME"),
            (("network", "reset"), "configure NAME"),
            (("network", "list", "extra"), "configure NAME"),
            (("network", "configure"), "configure NAME"),
            (("network", "configure", "eth0", "extra"), "configure NAME"),
            (("network", "configure", "eth0", "--dhcp", "--ip", "10.0.0.2"), "--dhcp | --ip"),
            (("network", "configure", "eth0", "--prefix", "16"), "--dhcp | --ip"),
            (("network", "configure", "eth0", "--dhcp", "--prefix", "16"), "--dhcp | --ip"),
            (("network", "configure", "eth0", "--gateway", "10.0.0.1"), "--dhcp | --ip"),
            (("network", "list", "--dhcp"), "--dhcp | --ip"),
            (("network", "list", "--ip", "10.0.0.2"), "--dhcp | --ip"),
            (("network", "list", "--dns", "1.1.1.1"), "--dhcp | --ip"),
            (("system", "reboot"), "status|logs|processes"),
            (("system", "logs", "extra"), "status|logs|processes"),
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv):
                code, data, _, _ = self.run_cli(*argv, "--yes")
                self.assertEqual(code, 1)
                self.assertIn("usage:", data["error"])
                self.assertIn(expected, data["error"])
        self.service.assert_not_called()


class InstallPackCliTests(LabCliCase):
    ARGV = ("toolkit", "install-pack", PACK_DIR, "--signers", SIGNERS)

    def test_the_parser_takes_the_path_signers_and_yes(self):
        a = build_parser().parse_args(["lab", *self.ARGV, "--yes"])
        self.assertEqual((a.lab_action, a.args, a.signers, a.yes), ("toolkit", ["install-pack", PACK_DIR], SIGNERS, True))

    def test_signers_is_accepted_before_the_action_and_is_off_unless_given(self):
        a = build_parser().parse_args(["--json", "lab", "--signers", SIGNERS, "toolkit", "install-pack", PACK_DIR])
        self.assertEqual((a.signers, a.args), (SIGNERS, ["install-pack", PACK_DIR]))
        self.assertIsNone(build_parser().parse_args(["lab", "toolkit", "list"]).signers)

    def test_without_yes_it_is_refused_and_never_builds_a_service(self):
        code, data, _, _ = self.run_cli(*self.ARGV)
        self.assertEqual(code, 12)
        self.assertIn("--yes", data["error"])
        self.assertIn("driver pack", data["error"])
        self.service.assert_not_called()
        self.assertEqual(self.lab.method_calls, [])

    def test_with_yes_it_runs_that_one_method_with_the_path_and_the_signers(self):
        code, data, _, _ = self.run_cli(*self.ARGV, "--yes")
        self.assertEqual(code, 0, data)
        self.lab.toolkit_install_pack.assert_awaited_once_with(PACK_DIR, SIGNERS)
        self.assertEqual(len(self.lab.method_calls), 1)
        self.ble_run.assert_not_called()

    def test_it_loads_nothing_and_doesnt_claim_to_know(self):
        _, data, _, _ = self.run_cli(*self.ARGV, "--yes")
        self.assertEqual(data["toolkit"], {"installed": True, "loaded": None, "path": VENDOR_PATH, "error": ""})
        _, _, text, _ = self.run_cli(*self.ARGV, "--yes", json_mode=False)
        self.assertIn("no modules were loaded", text)
        self.assertIn(f"toolkit: installed at {VENDOR_PATH}", text)
        self.lab.toolkit_load.assert_not_called()

    def test_a_missing_path_signers_or_an_extra_argument_shows_usage_and_does_nothing(self):
        cases = [
            ("toolkit", "install-pack", "--signers", SIGNERS),
            ("toolkit", "install-pack", PACK_DIR),
            ("toolkit", "install-pack"),
            ("toolkit", "install-pack", PACK_DIR, "extra", "--signers", SIGNERS),
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                code, data, _, _ = self.run_cli(*argv, "--yes")
                self.assertEqual(code, 1)
                self.assertIn("usage:", data["error"])
                self.assertIn("install-pack PATH --signers PATH", data["error"])
        self.service.assert_not_called()

    def test_signers_belongs_to_install_pack_alone(self):
        for argv in (("toolkit", "install"), ("toolkit", "load"), ("status",), ("system",)):
            with self.subTest(argv=argv):
                code, data, _, _ = self.run_cli(*argv, "--signers", SIGNERS, "--yes")
                self.assertEqual(code, 1)
                self.assertIn("usage:", data["error"])
        self.service.assert_not_called()

    def test_usage_is_checked_before_the_confirmation(self):
        code, data, _, _ = self.run_cli("toolkit", "install-pack", PACK_DIR)
        self.assertEqual(code, 1)
        self.assertIn("usage:", data["error"])

    def test_a_refused_pack_is_a_toolkit_error(self):
        self.lab.toolkit_install_pack.side_effect = ToolkitError("the signature was not accepted, so the pack was refused")
        code, data, _, _ = self.run_cli(*self.ARGV, "--yes")
        self.assertEqual(code, 13)
        self.assertIn("pack was refused", data["error"])
        code, _, _, err = self.run_cli(*self.ARGV, "--yes", json_mode=False)
        self.assertEqual(code, 13)
        self.assertIn("pack was refused", err)

    def test_install_and_the_other_toolkit_actions_are_unchanged(self):
        self.run_cli("toolkit", "install", "--yes")
        self.lab.toolkit_install.assert_awaited_once_with()
        self.lab.toolkit_install_pack.assert_not_called()


class InstallPackUrlCliTests(LabCliCase):
    URL = "https://packs.example/vendor-pack.zip"
    ARGV = ("toolkit", "install-pack-url", URL, "--sha256", SHA, "--signers", SIGNERS)

    def setUp(self):
        super().setUp()
        self.lab.toolkit_install_pack_url.return_value = PackState(installed=True, path=VENDOR_PATH)

    def test_the_parser_takes_the_url_hash_signers_and_yes(self):
        a = build_parser().parse_args(["lab", *self.ARGV, "--yes"])
        self.assertEqual(
            (a.lab_action, a.args, a.sha256, a.signers, a.yes),
            ("toolkit", ["install-pack-url", self.URL], SHA, SIGNERS, True),
        )

    def test_the_hash_is_off_unless_given(self):
        self.assertIsNone(build_parser().parse_args(["lab", "toolkit", "list"]).sha256)

    def test_without_yes_it_is_refused_and_never_builds_a_service(self):
        code, data, _, _ = self.run_cli(*self.ARGV)
        self.assertEqual(code, 12)
        self.assertIn("--yes", data["error"])
        self.assertIn("downloads a driver pack", data["error"])
        self.service.assert_not_called()
        self.assertEqual(self.lab.method_calls, [])

    def test_with_yes_it_runs_that_one_method_with_the_url_the_hash_and_the_signers(self):
        code, data, _, _ = self.run_cli(*self.ARGV, "--yes")
        self.assertEqual(code, 0, data)
        self.lab.toolkit_install_pack_url.assert_awaited_once_with(self.URL, SHA, SIGNERS)
        self.assertEqual(len(self.lab.method_calls), 1)
        self.lab.toolkit_install_pack.assert_not_called()
        self.ble_run.assert_not_called()

    def test_a_file_url_is_taken_too(self):
        url = "file:///home/me/vendor-pack.zip"
        code, data, _, _ = self.run_cli("toolkit", "install-pack-url", url, "--sha256", SHA, "--signers", SIGNERS, "--yes")
        self.assertEqual(code, 0, data)
        self.lab.toolkit_install_pack_url.assert_awaited_once_with(url, SHA, SIGNERS)

    def test_it_loads_nothing_and_doesnt_claim_to_know(self):
        _, data, _, _ = self.run_cli(*self.ARGV, "--yes")
        self.assertEqual(data["toolkit"], {"installed": True, "loaded": None, "path": VENDOR_PATH, "error": ""})
        _, _, text, _ = self.run_cli(*self.ARGV, "--yes", json_mode=False)
        self.assertIn("pack downloaded", text)
        self.assertIn("no modules were loaded", text)
        self.assertIn(f"toolkit: installed at {VENDOR_PATH}", text)
        self.lab.toolkit_load.assert_not_called()

    def test_a_missing_url_hash_or_signers_or_an_extra_argument_shows_usage_and_does_nothing(self):
        cases = [
            ("toolkit", "install-pack-url", "--sha256", SHA, "--signers", SIGNERS),
            ("toolkit", "install-pack-url", self.URL, "--signers", SIGNERS),
            ("toolkit", "install-pack-url", self.URL, "--sha256", SHA),
            ("toolkit", "install-pack-url"),
            ("toolkit", "install-pack-url", self.URL, "extra", "--sha256", SHA, "--signers", SIGNERS),
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                code, data, _, _ = self.run_cli(*argv, "--yes")
                self.assertEqual(code, 1)
                self.assertIn("usage:", data["error"])
                self.assertIn("install-pack-url URL --sha256 SHA --signers PATH", data["error"])
        self.service.assert_not_called()

    def test_the_hash_belongs_to_install_pack_url_alone(self):
        for argv in (
            ("toolkit", "install-pack", PACK_DIR, "--signers", SIGNERS),
            ("toolkit", "install"),
            ("toolkit", "load"),
            ("status",),
        ):
            with self.subTest(argv=argv):
                code, data, _, _ = self.run_cli(*argv, "--sha256", SHA, "--yes")
                self.assertEqual(code, 1)
                self.assertIn("usage:", data["error"])
        self.service.assert_not_called()

    def test_signers_is_accepted_for_both_pack_installs_and_for_no_other(self):
        code, _, _, _ = self.run_cli("toolkit", "install-pack-url", self.URL, "--sha256", SHA, "--signers", SIGNERS, "--yes")
        self.assertEqual(code, 0)
        code, data, _, _ = self.run_cli("toolkit", "list", "--signers", SIGNERS)
        self.assertEqual(code, 1)
        self.assertIn("usage:", data["error"])

    def test_http_and_other_urls_are_refused_before_the_confirmation_and_before_any_service(self):
        for url in ("http://packs.example/vendor-pack.zip", "ftp://packs.example/vendor-pack.zip", "packs.example/v.zip"):
            for extra in ((), ("--yes",)):
                with self.subTest(url=url, extra=extra):
                    argv = ("toolkit", "install-pack-url", url, "--sha256", SHA, "--signers", SIGNERS, *extra)
                    code, data, _, _ = self.run_cli(*argv)
                    self.assertEqual(code, 13)
                    self.assertIn("https:// or file://", data["error"])
        self.service.assert_not_called()
        self.assertEqual(self.lab.method_calls, [])

    def test_a_hash_that_is_not_one_is_refused_before_any_service(self):
        for bad in ("abc", SHA + "0", "sha256:" + SHA):
            with self.subTest(bad):
                argv = ("toolkit", "install-pack-url", self.URL, "--sha256", bad, "--signers", SIGNERS, "--yes")
                code, data, _, _ = self.run_cli(*argv)
                self.assertEqual(code, 13)
                self.assertIn("64 hex digits", data["error"])
        self.service.assert_not_called()

    def test_a_refused_pack_is_a_toolkit_error(self):
        self.lab.toolkit_install_pack_url.side_effect = ToolkitError("the download has another sha256, so the pack was refused")
        code, data, _, _ = self.run_cli(*self.ARGV, "--yes")
        self.assertEqual(code, 13)
        self.assertIn("pack was refused", data["error"])
        code, _, _, err = self.run_cli(*self.ARGV, "--yes", json_mode=False)
        self.assertEqual(code, 13)
        self.assertIn("pack was refused", err)

    def test_the_local_pack_install_is_unchanged(self):
        self.run_cli("toolkit", "install-pack", PACK_DIR, "--signers", SIGNERS, "--yes")
        self.lab.toolkit_install_pack.assert_awaited_once_with(PACK_DIR, SIGNERS)
        self.lab.toolkit_install_pack_url.assert_not_called()


class StatusOutputTests(LabCliCase):
    def test_json_carries_each_part_and_says_nothing_about_loaded(self):
        self.lab.status.return_value = LabStatus(
            host_mode=PORT_ON, devices=[AUDIO], toolkit=PackState(installed=True, path=PACK_PATH)
        )
        _, data, _, _ = self.run_cli("status")
        self.assertTrue(data["host_mode"]["enabled"])
        self.assertEqual(data["devices"][0]["kind"], "audio")
        self.assertEqual(data["toolkit"]["path"], PACK_PATH)
        self.assertIsNone(data["toolkit"]["loaded"])

    def test_text_names_the_port_the_toolkit_and_the_devices(self):
        self.lab.status.return_value = LabStatus(
            host_mode=PORT_ON, devices=[AUDIO], toolkit=PackState(installed=True, path=PACK_PATH)
        )
        _, _, text, _ = self.run_cli("status", json_mode=False)
        self.assertIn("host mode: on · root hubs usb1, usb2", text)
        self.assertIn(f"toolkit: installed at {PACK_PATH}", text)
        self.assertIn("05ac:110a  audio", text)
        self.assertNotIn("loaded", text)

    def test_text_with_no_devices_says_so(self):
        _, _, text, _ = self.run_cli("status", json_mode=False)
        self.assertIn("host mode: off", text)
        self.assertIn("no USB devices", text)

    def test_a_device_list_that_failed_is_a_note_not_an_empty_bus(self):
        self.lab.status.return_value = LabStatus(host_mode=PORT_ON, error="could not list USB devices")
        code, _, text, _ = self.run_cli("status", json_mode=False)
        self.assertEqual(code, 0)
        self.assertIn("note: could not list USB devices", text)
        self.assertNotIn("no USB devices", text)

    def test_an_unfinished_switch_points_at_recover(self):
        stuck = HostModeState(supported=True, hostmode="1", partial=True, paused=["gpUsb"], marker="switching")
        self.lab.status.return_value = LabStatus(host_mode=stuck)
        _, _, text, _ = self.run_cli("status", json_mode=False)
        self.assertIn("between modes", text)
        self.assertIn("paused gpUsb", text)
        self.assertIn("quak lab host recover", text)

    def test_between_modes_without_quaks_marker_does_not_point_at_recover(self):
        foreign = HostModeState(supported=True, hostmode="2", partial=True)
        self.lab.status.return_value = LabStatus(host_mode=foreign)
        _, _, text, _ = self.run_cli("status", json_mode=False)
        self.assertIn("between modes", text)
        self.assertNotIn("recover", text)

    def test_a_camera_without_the_switch_reports_that_rather_than_off(self):
        self.lab.status.return_value = LabStatus(host_mode=HostModeState(error="this camera has no USB host-mode switch"))
        _, _, text, _ = self.run_cli("status", json_mode=False)
        self.assertIn("host mode: this camera has no USB host-mode switch", text)
        self.assertNotIn("host mode: off", text)


class HostOutputTests(LabCliCase):
    def test_json_has_the_state_the_steps_and_the_message(self):
        _, data, _, _ = self.run_cli("host", "on", "--yes")
        self.assertTrue(data["state"]["enabled"])
        self.assertEqual(data["steps"], ["paused gpUsb", "hostmode=1"])
        self.assertEqual(data["message"], "Host mode is on")

    def test_text_has_the_message_and_the_port(self):
        _, _, text, _ = self.run_cli("host", "recover", json_mode=False)
        self.assertIn("Nothing to recover", text)
        self.assertIn("host mode: off · gadget on 19880000.dwc3", text)

    def test_a_result_that_could_not_be_read_back_is_shown(self):
        unread = HostModeState(error="the switch finished, but its result could not be read back: lost")
        self.lab.host_on.return_value = HostModeResult(unread, ["hostmode=1"], "Host mode is on")
        _, _, text, _ = self.run_cli("host", "on", "--yes", json_mode=False)
        self.assertIn("could not be read back", text)


class DevicesOutputTests(LabCliCase):
    def test_json_adds_the_kind_and_keeps_the_interfaces(self):
        _, data, _, _ = self.run_cli("devices")
        device = data["devices"][0]
        self.assertEqual((device["id"], device["kind"]), ("1-1", "audio"))
        self.assertEqual(device["interfaces"][0]["driver"], "snd-usb-audio")

    def test_text_is_one_line_per_device_with_the_interface_driver(self):
        _, _, text, _ = self.run_cli("devices", json_mode=False)
        self.assertEqual(text, "1-1  05ac:110a  audio · Apple USB-C Adapter · 480 Mb/s · snd-usb-audio\n")

    def test_a_device_with_only_an_id_still_prints(self):
        self.lab.devices.return_value = [UsbDevice(id="usb1")]
        _, _, text, _ = self.run_cli("devices", json_mode=False)
        self.assertEqual(text, "usb1  ----:----  other\n")

    def test_none_points_at_host_mode(self):
        self.lab.devices.return_value = []
        _, data, text, _ = self.run_cli("devices", json_mode=False)
        self.assertEqual(data, {})
        self.assertIn("quak lab host on --yes", text)
        _, data, _, _ = self.run_cli("devices")
        self.assertEqual(data, {"devices": []})


class ToolkitOutputTests(LabCliCase):
    def test_list_does_not_claim_the_modules_are_unloaded(self):
        _, data, _, _ = self.run_cli("toolkit", "list")
        self.assertEqual(data["toolkit"], {"installed": True, "loaded": None, "path": PACK_PATH, "error": ""})
        _, _, text, _ = self.run_cli("toolkit", "list", json_mode=False)
        self.assertEqual(text, f"toolkit: installed at {PACK_PATH}\n")

    def test_install_does_not_claim_it_either_and_says_it_loaded_nothing(self):
        _, data, _, _ = self.run_cli("toolkit", "install", "--yes")
        self.assertIsNone(data["toolkit"]["loaded"])
        _, _, text, _ = self.run_cli("toolkit", "install", "--yes", json_mode=False)
        self.assertIn("no modules were loaded", text)

    def test_load_unload_and_remove_do_know(self):
        for argv, loaded in ((("load",), True), (("unload",), False), (("remove", "--yes"), False)):
            with self.subTest(argv=argv):
                _, data, _, _ = self.run_cli("toolkit", *argv)
                self.assertIs(data["toolkit"]["loaded"], loaded)

    def test_a_removed_pack_reads_as_not_installed(self):
        _, _, text, _ = self.run_cli("toolkit", "remove", "--yes", json_mode=False)
        self.assertEqual(text, "toolkit removed\ntoolkit: not installed\n")

    def test_a_problem_on_the_card_is_shown(self):
        self.lab.toolkit_status.return_value = PackState(path=PACK_PATH, error="core is missing tinycap")
        code, data, _, _ = self.run_cli("toolkit", "list")
        self.assertEqual(code, 0)
        self.assertEqual(data["toolkit"]["error"], "core is missing tinycap")
        _, _, text, _ = self.run_cli("toolkit", "list", json_mode=False)
        self.assertEqual(text, "toolkit: core is missing tinycap\n")


class StorageOutputTests(LabCliCase):
    def test_volumes(self):
        _, data, _, _ = self.run_cli("storage", "list")
        self.assertEqual(data["volumes"][0]["device"], "/dev/sda1")
        _, _, text, _ = self.run_cli("storage", "list", json_mode=False)
        self.assertEqual(text, '/dev/sda1  vfat  "CARD"  32.0 GB  not mounted\n')

    def test_a_mounted_volume_says_where_and_how(self):
        self.lab.storage_list.return_value = [
            Volume("/dev/sda1", fs="vfat", size=2_000, mountpoint=MOUNT, mounted=True, read_only=False)
        ]
        _, _, text, _ = self.run_cli("storage", "list", json_mode=False)
        self.assertEqual(text, f"/dev/sda1  vfat  2.0 KB  mounted at {MOUNT} (read-write)\n")

    def test_no_volumes(self):
        self.lab.storage_list.return_value = []
        _, _, text, _ = self.run_cli("storage", "list", json_mode=False)
        self.assertEqual(text, "no volumes found\n")

    def test_a_folder(self):
        _, data, _, _ = self.run_cli("storage", "list", MOUNT)
        self.assertEqual(data["path"], MOUNT)
        self.assertEqual(data["entries"][0], {"name": "clip.wav", "type": "file", "size": 2_000_000})
        _, _, text, _ = self.run_cli("storage", "list", MOUNT, json_mode=False)
        self.assertIn("file     2.0 MB  clip.wav", text)
        self.assertIn("old", text)

    def test_an_empty_folder(self):
        self.lab.storage_list_dir.return_value = []
        _, _, text, _ = self.run_cli("storage", "list", MOUNT, json_mode=False)
        self.assertEqual(text, f"{MOUNT} is empty\n")

    def test_mount_reports_what_the_kernel_said_and_nothing_it_does_not_know(self):
        _, data, _, _ = self.run_cli("storage", "mount", "sda1", "--yes")
        self.assertEqual(data, {"device": "/dev/sda1", "fs": "vfat", "mountpoint": MOUNT, "read_only": True})
        _, _, text, _ = self.run_cli("storage", "mount", "sda1", "--yes", json_mode=False)
        self.assertEqual(text, f"mounted /dev/sda1 at {MOUNT} (read-only)\n")

    def test_umount_and_eject(self):
        _, data, _, _ = self.run_cli("storage", "umount", MOUNT, "--yes")
        self.assertEqual(data, {"unmounted": MOUNT})
        _, data, _, _ = self.run_cli("storage", "eject", MOUNT, "--yes")
        self.assertEqual(data, {"ejected": MOUNT})

    def test_copy_of_a_file_shows_its_hash(self):
        _, data, _, _ = self.run_cli("storage", "copy", SRC, DST, "--yes")
        self.assertEqual(data, {"target": DST, "sha256": SHA})
        _, _, text, _ = self.run_cli("storage", "copy", SRC, DST, "--yes", json_mode=False)
        self.assertIn(f"copied to {DST} (sha256 {SHA} on both ends)", text)

    def test_copy_of_a_folder_says_it_is_not_verified(self):
        self.lab.storage_copy.return_value = CopyResult(DST, None)
        _, data, _, _ = self.run_cli("storage", "copy", SRC, DST, "--yes")
        self.assertIsNone(data["sha256"])
        _, _, text, _ = self.run_cli("storage", "copy", SRC, DST, "--yes", json_mode=False)
        self.assertIn("aren't verified", text)
        self.assertNotIn("sha256", text)

    def test_progress_stays_out_of_json(self):
        _, _, text, _ = self.run_cli("storage", "copy", SRC, DST, "--yes")
        self.assertNotIn("copying", text)


class NetworkParserTests(LabCliCase):
    def test_the_static_options_parse(self):
        a = build_parser().parse_args(
            ["lab", "network", "configure", "eth0", "--ip", "10.0.0.2", "--prefix", "16", "--gateway", "10.0.0.1",
             "--dns", "1.1.1.1", "--dns", "8.8.8.8", "--yes"]
        )
        self.assertEqual((a.lab_action, a.args), ("network", ["configure", "eth0"]))
        self.assertEqual((a.ip, a.prefix, a.gateway), ("10.0.0.2", 16, "10.0.0.1"))
        self.assertEqual(a.dns, ["1.1.1.1", "8.8.8.8"])
        self.assertTrue(a.yes)

    def test_nothing_is_set_unless_given(self):
        a = build_parser().parse_args(["lab", "network", "configure", "eth0"])
        self.assertEqual((a.dhcp, a.ip, a.prefix, a.gateway, a.dns, a.yes), (False, None, None, None, None, False))

    def test_a_prefix_that_is_not_a_number_is_refused_by_the_parser(self):
        code, _, _, err = self.run_cli(
            "network", "configure", "eth0", "--ip", "10.0.0.2", "--prefix", "wide", "--yes", json_mode=False
        )
        self.assertEqual(code, 2)
        self.assertIn("--prefix", err)
        self.service.assert_not_called()


class NetworkConfigureTests(LabCliCase):
    def test_dhcp_is_the_default_and_dhcp_says_the_same(self):
        for extra in ((), ("--dhcp",)):
            with self.subTest(extra=extra):
                self.reset()
                code, data, _, _ = self.run_cli("network", "configure", "eth0", *extra, "--yes")
                self.assertEqual(code, 0, data)
                self.lab.network_configure.assert_awaited_once_with("eth0", dhcp=True)

    def test_dhcp_can_still_set_dns(self):
        code, data, _, _ = self.run_cli("network", "configure", "eth0", "--dns", "1.1.1.1", "--yes")
        self.assertEqual(code, 0, data)
        self.lab.network_configure.assert_awaited_once_with("eth0", dhcp=True, dns=["1.1.1.1"])

    def test_ip_turns_dhcp_off_and_leaves_the_prefix_to_its_default(self):
        code, data, _, _ = self.run_cli("network", "configure", "eth0", "--ip", "10.0.0.2", "--yes")
        self.assertEqual(code, 0, data)
        self.lab.network_configure.assert_awaited_once_with("eth0", dhcp=False, address="10.0.0.2")

    def test_every_static_option_arrives(self):
        code, data, _, _ = self.run_cli(
            "network", "configure", "eth0", "--ip", "10.0.0.2", "--prefix", "16", "--gateway", "10.0.0.1",
            "--dns", "1.1.1.1", "--dns", "8.8.8.8", "--yes",
        )
        self.assertEqual(code, 0, data)
        self.lab.network_configure.assert_awaited_once_with(
            "eth0", dhcp=False, address="10.0.0.2", prefix=16, gateway="10.0.0.1", dns=["1.1.1.1", "8.8.8.8"]
        )

    def test_an_empty_ip_stays_a_static_request_rather_than_becoming_dhcp(self):
        self.run_cli("network", "configure", "eth0", "--ip", "", "--yes")
        self.lab.network_configure.assert_awaited_once_with("eth0", dhcp=False, address="")

    def test_without_yes_nothing_happens_whatever_the_options(self):
        for extra in ((), ("--dhcp",), ("--ip", "10.0.0.2", "--gateway", "10.0.0.1")):
            with self.subTest(extra=extra):
                code, data, _, _ = self.run_cli("network", "configure", "eth0", *extra)
                self.assertEqual(code, 12)
                self.assertIn("--yes", data["error"])
        self.service.assert_not_called()
        self.assertEqual(self.lab.method_calls, [])

    def test_list_needs_no_yes(self):
        code, data, _, _ = self.run_cli("network", "list")
        self.assertEqual(code, 0, data)

    def test_address_still_picks_the_camera(self):
        self.run_cli("network", "configure", "eth0", "--ip", "10.0.0.2", "--yes")
        self.assertEqual(self.service.call_args.args[1], ADDRESS)

    def test_an_ip_given_to_address_is_refused_rather_than_taken_for_a_camera(self):
        for address in ("10.0.0.2", "10.0.0.300"):
            with self.subTest(address=address):
                code, data, _, _ = self.run_cli("network", "configure", "eth0", "--yes", address=address)
                self.assertEqual(code, 1)
                self.assertIn("--ip", data["error"])
        self.service.assert_not_called()

    def test_a_refusal_from_the_camera_side_keeps_its_exit_code(self):
        self.lab.network_configure.side_effect = LabError("refusing lo: it is the loopback interface")
        code, data, _, _ = self.run_cli("network", "configure", "lo", "--yes")
        self.assertEqual(code, 12)
        self.assertEqual(data["error"], "refusing lo: it is the loopback interface")

    def test_a_lost_link_says_the_change_may_have_gone_through(self):
        self.lab.network_configure.side_effect = NetworkError("lost the camera")
        code, data, _, _ = self.run_cli("network", "configure", "eth0", "--yes")
        self.assertEqual(code, 6)
        self.assertIn("lost the camera", data["error"])
        self.assertIn("may have gone through", data["error"])
        self.assertIn("quak lab network list", data["error"])


class NetworkOutputTests(LabCliCase):
    def test_list_json_is_every_field_of_every_interface(self):
        _, data, _, _ = self.run_cli("network", "list")
        self.assertEqual([interface["name"] for interface in data["interfaces"]], ["lo", "eth0", "wlan0"])
        self.assertEqual(
            data["interfaces"][1],
            {
                "name": "eth0",
                "mac": "11:22:33:44:55:66",
                "up": True,
                "kind": "ethernet",
                "addresses": ["10.0.0.2/24"],
                "driver": "r8152",
            },
        )

    def test_list_text_is_one_line_per_interface(self):
        _, _, text, _ = self.run_cli("network", "list", json_mode=False)
        self.assertEqual(
            text,
            "lo  other  up  127.0.0.1/8\n"
            "eth0  ethernet  up  11:22:33:44:55:66  10.0.0.2/24  r8152\n"
            "wlan0  wifi  down  AA:BB:CC:DD:EE:FF  no address\n",
        )

    def test_list_with_no_interfaces(self):
        self.lab.network_list.return_value = []
        _, data, text, _ = self.run_cli("network", "list", json_mode=False)
        self.assertEqual(data, {})
        self.assertEqual(text, "no network interfaces found\n")
        _, data, _, _ = self.run_cli("network", "list")
        self.assertEqual(data, {"interfaces": []})

    def test_dhcp_json_does_not_claim_what_the_lease_brought(self):
        _, data, _, _ = self.run_cli("network", "configure", "eth0", "--yes")
        self.assertEqual(
            data, {"interface": "eth0", "mode": "dhcp", "addresses": ["10.0.0.7/24"], "gateway": None, "dns": None}
        )

    def test_dhcp_text_leaves_out_the_gateway_and_dns(self):
        _, _, text, _ = self.run_cli("network", "configure", "eth0", "--yes", json_mode=False)
        self.assertEqual(text, "eth0 set up by DHCP: 10.0.0.7/24. The address lasts until the camera restarts\n")

    def test_static_text_shows_what_was_set(self):
        self.lab.network_configure.return_value = {
            "interface": "eth0",
            "mode": "static",
            "addresses": ["10.0.0.2/16"],
            "gateway": "10.0.0.1",
            "dns": ["1.1.1.1", "8.8.8.8"],
        }
        _, _, text, _ = self.run_cli("network", "configure", "eth0", "--ip", "10.0.0.2", "--yes", json_mode=False)
        self.assertEqual(
            text,
            "eth0 set up by hand: 10.0.0.2/16. The address lasts until the camera restarts\n"
            "gateway 10.0.0.1\n"
            "DNS 1.1.1.1, 8.8.8.8\n",
        )


class SystemOutputTests(LabCliCase):
    def test_json_is_the_snapshot(self):
        _, data, _, _ = self.run_cli("system")
        self.assertEqual(data["uptime"], 3725.5)
        self.assertEqual(data["df"]["percent"], 7)

    def test_text_is_readable(self):
        _, _, text, _ = self.run_cli("system", json_mode=False)
        self.assertEqual(
            text,
            "up 1:02:05\n"
            "load 0.5 0.25 0.1\n"
            "memory: total 1.0 GB, available 500.0 MB, free 200.0 MB\n"
            "SD card: 30.0 GB free of 32.0 GB (7% used)\n"
            "cpu: 42.5 °C\n",
        )

    def test_what_the_camera_could_not_say_is_left_out(self):
        self.lab.system_status.return_value = {"uptime": None, "load": [], "mem": {}, "temp": [], "df": {}}
        code, _, text, _ = self.run_cli("system", json_mode=False)
        self.assertEqual(code, 0)
        self.assertEqual(text, "the camera reported nothing\n")

    def test_a_zero_uptime_is_still_an_uptime(self):
        self.lab.system_status.return_value = {"uptime": 0.0, "load": [], "mem": {}, "temp": [], "df": {}}
        _, _, text, _ = self.run_cli("system", json_mode=False)
        self.assertEqual(text, "up 0:00:00\n")

    def test_a_sensor_without_a_type_goes_by_its_zone(self):
        self.lab.system_status.return_value = {
            "uptime": None, "load": [], "mem": {}, "df": {},
            "temp": [{"zone": "thermal_zone3", "type": "", "celsius": 30.0}],
        }
        _, _, text, _ = self.run_cli("system", json_mode=False)
        self.assertEqual(text, "thermal_zone3: 30.0 °C\n")

    def test_logs_and_processes(self):
        _, data, _, _ = self.run_cli("system", "logs")
        self.assertEqual(data, {"logs": "usb 1-1: new device\n"})
        _, _, text, _ = self.run_cli("system", "logs", json_mode=False)
        self.assertIn("usb 1-1: new device", text)
        _, data, _, _ = self.run_cli("system", "processes")
        self.assertEqual(data, {"processes": "PID USER COMMAND\n"})
        _, _, text, _ = self.run_cli("system", "processes", json_mode=False)
        self.assertIn("PID USER COMMAND", text)


class ErrorTests(LabCliCase):
    def test_each_failure_keeps_its_own_exit_code(self):
        cases = [(LabError("lab"), 12), (ToolkitError("toolkit"), 13), (HostModeError("port"), 14), (NetworkError("lost"), 6)]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                self.lab.status.side_effect = error
                code, data, _, _ = self.run_cli("status")
                self.assertEqual(code, expected)
                self.assertEqual(data["error"], str(error))

    def test_text_mode_puts_the_error_on_stderr(self):
        self.lab.toolkit_load.side_effect = ToolkitError("wrong kernel: nothing was loaded")
        code, _, text, err = self.run_cli("toolkit", "load", json_mode=False)
        self.assertEqual(code, 13)
        self.assertEqual(text, "")
        self.assertIn("error: wrong kernel: nothing was loaded", err)


class PasswordStdinTests(LabCliCase):
    def run_piped(self, text, *argv, **kwargs):
        """``run_cli`` with ``text`` waiting on stdin: its results, then whatever was left unread."""
        stdin = io.StringIO(text)
        with mock.patch("sys.stdin", stdin):
            result = self.run_cli(*argv, **kwargs)
        return result, stdin.read()

    def password_profile(self):
        profiles.save(SshProfile(address=ADDRESS, auth="password"))

    def test_it_is_off_unless_asked_for(self):
        parse = build_parser().parse_args
        self.assertFalse(parse(["lab", "status"]).password_stdin)
        self.assertTrue(parse(["lab", "status", "--password-stdin"]).password_stdin)
        self.assertTrue(parse(["lab", "--password-stdin", "status"]).password_stdin)

    def test_one_line_goes_to_the_service_and_the_rest_stays_on_stdin(self):
        self.password_profile()
        (code, data, _, _), left = self.run_piped(f"{SECRET}\nnext line\n", "status", "--password-stdin")
        self.assertEqual(code, 0, data)
        self.assertEqual(self.service.call_args.kwargs["password"], SECRET)
        self.assertEqual(self.service.call_args.kwargs["profile"].auth, "password")
        self.assertEqual(left, "next line\n")

    def test_only_the_line_ending_comes_off(self):
        self.password_profile()
        for ending in ("\n", "\r\n", ""):
            with self.subTest(ending=ending):
                self.reset()
                self.run_piped(f" {SECRET} {ending}", "status", "--password-stdin")
                self.assertEqual(self.service.call_args.kwargs["password"], f" {SECRET} ")

    def test_it_is_never_shown(self):
        self.password_profile()
        for json_mode in (True, False):
            with self.subTest(json_mode=json_mode):
                self.lab.status.side_effect = None
                (_, _, text, err), _ = self.run_piped(f"{SECRET}\n", "status", "--password-stdin", json_mode=json_mode)
                self.assertNotIn(SECRET, text)
                self.assertNotIn(SECRET, err)
                self.lab.status.side_effect = LabError("it failed")
                (code, _, text, err), _ = self.run_piped(
                    f"{SECRET}\n", "status", "--password-stdin", json_mode=json_mode
                )
                self.assertEqual(code, 12)
                self.assertNotIn(SECRET, text)
                self.assertNotIn(SECRET, err)

    def test_it_is_never_saved(self):
        self.password_profile()
        self.run_piped(f"{SECRET}\n", "status", "--password-stdin")
        self.assertEqual(profiles.load(ADDRESS), SshProfile(address=ADDRESS, auth="password"))
        files = [path for path in Path(os.environ["QUAK_CONFIG_DIR"]).rglob("*") if path.is_file()]
        self.assertTrue(files)
        for path in files:
            self.assertNotIn(SECRET.encode(), path.read_bytes(), path)

    def test_an_empty_line_spends_no_login(self):
        self.password_profile()
        for text in ("", "\n", "\r\n"):
            with self.subTest(text=text):
                (code, data, _, _), _ = self.run_piped(text, "status", "--password-stdin")
                self.assertEqual(code, 8)
                self.assertIn("--password-stdin", data["error"])
        self.service.assert_not_called()

    def test_without_the_option_stdin_is_left_alone(self):
        self.password_profile()
        _, left = self.run_piped(f"{SECRET}\n", "status")
        self.assertEqual(left, f"{SECRET}\n")
        self.assertNotIn("password", self.service.call_args.kwargs)

    def test_a_profile_that_doesnt_use_one_leaves_stdin_alone_and_gets_none(self):
        for auth in ("key", "none"):
            with self.subTest(auth=auth):
                self.reset()
                profiles.save(SshProfile(address=ADDRESS, auth=auth))
                _, left = self.run_piped(f"{SECRET}\n", "status", "--password-stdin")
                self.assertEqual(left, f"{SECRET}\n")
                self.assertNotIn("password", self.service.call_args.kwargs)

    def test_a_refused_action_reads_nothing(self):
        self.password_profile()
        (code, _, _, _), left = self.run_piped(f"{SECRET}\n", "host", "on", "--password-stdin")
        self.assertEqual(code, 12)
        self.assertEqual(left, f"{SECRET}\n")
        self.service.assert_not_called()

    def test_the_real_service_takes_it_to_the_route(self):
        self.password_profile()
        seen = []

        @contextlib.asynccontextmanager
        async def remote_factory(manager, address, *, profile=None, password=None):
            seen.append(password)
            yield mock.sentinel.remote

        self.service.side_effect = lambda manager, address, profile=None, password=None: LabService(
            manager, address, profile, remote_factory, password
        )
        with mock.patch("quak.lab.devices.list_devices", return_value=[]):
            (code, data, _, _), _ = self.run_piped(f"{SECRET}\n", "devices", "--password-stdin")
        self.assertEqual(code, 0, data)
        self.assertEqual(seen, [SECRET])


class UsbNcmRefusalTests(LabCliCase):
    """The real service behind the real CLI, with only the link to the camera faked."""

    def host_on_over(self, endpoint):
        sent = []

        class FakeRemote:
            def __init__(self):
                self.endpoint = endpoint

            def exec(self, command, input=None, timeout=0.0):
                sent.append(command)
                raise AssertionError("the refusal must come before anything reaches the camera")

        @contextlib.asynccontextmanager
        async def remote_factory(manager, address, *, profile=None):
            yield FakeRemote()

        self.service.side_effect = lambda manager, address, profile=None: LabService(
            manager, address, profile, remote_factory
        )
        code, data, _, _ = self.run_cli("host", "on", "--yes")
        return code, data, sent

    def test_host_on_over_the_cameras_usb_is_refused_before_anything_is_sent(self):
        code, data, sent = self.host_on_over(Endpoint("172.20.0.51", 22, 80, "usb"))
        self.assertEqual(code, 14)
        self.assertIn("Wi-Fi or Ethernet", data["error"])
        self.assertIn("Nothing was changed", data["error"])
        self.assertEqual(sent, [])

    def test_the_refusal_also_covers_a_hand_typed_address_on_the_cameras_usb(self):
        camera_usb = UsbNet("usb0", "172.20.0.50", "2672", "0059")
        with mock.patch("quak.ssh.transport.find_usb_nets", return_value=[camera_usb]):
            code, data, sent = self.host_on_over(Endpoint("172.20.0.51", 22, 80, "host"))
        self.assertEqual(code, 14)
        self.assertIn("Wi-Fi or Ethernet", data["error"])
        self.assertEqual(sent, [])

    def test_a_link_that_does_not_use_the_cameras_usb_gets_past_the_refusal(self):
        with mock.patch("quak.ssh.transport.find_usb_nets", return_value=[]):
            with mock.patch("quak.lab.hostmode.enable", return_value=HostModeResult(PORT_ON, [], "Host mode is on")) as on:
                code, data, sent = self.host_on_over(Endpoint(HOST, 22, 80, "host"))
        self.assertEqual(code, 0, data)
        on.assert_called_once()
        self.assertEqual(data["message"], "Host mode is on")
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
