"""The Lab tab, against a simulator-backed service with LabService and SshManager mocked.

No real ssh, no subprocess and no network: ``asyncSetUp`` makes all three fail the test if anything reaches for them.
The service is simulated but the App is told it isn't in demo mode, which is what makes the actions available.
"""

import ast
import asyncio
import json
import os
import re
import tempfile
import unittest
from dataclasses import replace
from unittest import mock

from quak.connect import Target
from quak.core.errors import (
    AdapterInUse,
    HostModeError,
    LabError,
    NetworkError,
    QuakError,
    SshError,
    ToolkitError,
)
from quak.core.registry import Status
from quak.lab.models import (
    AudioDevice,
    Framebuffer,
    HostModeResult,
    HostModeState,
    LabStatus,
    NetworkInterface,
    PackState,
    SerialPort,
    UsbDevice,
    UsbInterface,
    Volume,
)
from quak.lab.service import LabService
from quak.service import CameraService
from quak.ssh.manager import SshManager
from quak.ssh.models import SshProfile
from quak.transport.sim import SimCamera
from tests.test_i18n import PLACEHOLDER, ROOT, sync, untranslated_text

try:
    import flet as ft

    from quak.gui import app as gui_app
    from quak.gui import screens
except ImportError:  # GUI extra not installed
    ft = None

ADDRESS = "SIM"
SECRET = "correct horse battery"
TABS = ("overview", "usb", "audio", "storage", "network", "display", "serial", "system")
SCREEN = ROOT / "src/quak/gui/screens/lab.py"


def usb(id, product, iclass, driver, speed="480"):
    return UsbDevice(
        id=id,
        product=product,
        speed=speed,
        driver="usb",
        interfaces=[UsbInterface(f"{id}:1.0", iclass=iclass, driver=driver)],
    )


ROOT_HUB = usb("usb1", "xHCI Host Controller", "09", "hub", "5000")
MIC = usb("1-1", "Field Mic", "01", "snd-usb-audio", "12")
ETHERNET = usb("1-2", "Gigabit Adapter", "ff", "r8152", "5000")
SERIAL = usb("1-3", "FT232R", "ff", "ftdi_sio", "12")
WEBCAM = usb("1-4", "HD Webcam", "0e", "uvcvideo")
THUMB = usb("1-5", "", "08", "usb-storage")
DEVICES = [ROOT_HUB, MIC, ETHERNET, SERIAL, WEBCAM]

PORT_ON = HostModeState(enabled=True, supported=True, hostmode="1", devices=["usb1", "usb2"])
PORT_OFF = HostModeState(supported=True, hostmode="2", udc="gadget")
PORT_BETWEEN = HostModeState(supported=True, hostmode="1", partial=True)
PORT_STUCK = HostModeState(supported=True, hostmode="1", partial=True, marker="switching")
NO_SWITCH = HostModeState(error="this camera has no USB host-mode switch")
READY = LabStatus(host_mode=PORT_ON, devices=DEVICES, toolkit=PackState(installed=True, path="/tmp/fuse_d/quak/core"))

STICK = Volume("/dev/sda1", label="STICK", fs="vfat", size=8_000_000_000)
LAB_DISK = Volume(
    "/dev/sdb1", label="DATA", fs="exfat", size=64_000_000_000, mountpoint="/tmp/quak/mnt/sdb1", mounted=True
)
SD_CARD = Volume("/dev/mmcblk1p1", fs="", size=128_000_000_000, mountpoint="/tmp/fuse_d", mounted=True, read_only=False)
VOLUMES = [STICK, LAB_DISK, SD_CARD]

SYSTEM = {
    "uptime": 3725.0,
    "load": [0.5, 0.25, 0.1],
    "mem": {"total": 2_000_000_000, "available": 500_000_000},
    "temp": [{"zone": "thermal_zone0", "type": "cpu", "celsius": 41.5}],
    "df": {"total": 128_000_000_000, "used": 28_000_000_000, "free": 100_000_000_000, "percent": 22},
}
SYSTEM_SILENT = {"uptime": None, "load": [], "mem": {}, "temp": [], "df": {}}
LOGS = "[    1.0] usb 1-1: new device\n[    2.0] usb 1-1: Field Mic"

SPEAKER = AudioDevice(card=2, name="USB Speaker")  # it can't capture
SOUND_CARDS = [
    AudioDevice(card=1, name="Field Mic", capture_format="S16_LE", rates=[48000], channels=2),
    AudioDevice(card=1, name="Field Mic", capture_format="S24_3LE", rates=[48000], channels=2),
    SPEAKER,
    AudioDevice(card=3, name="Second Mic", capture_format="S16_LE", rates=[48000], channels=1),
]
SAVED = {"path": "/tmp/fuse_d/quak/recordings/quak-20261107-120000.wav", "size": 5_760_044}

INTERFACES = [
    NetworkInterface("lo", up=True, addresses=["127.0.0.1/8"]),
    NetworkInterface("eth0", up=True, kind="ethernet", addresses=["192.168.1.5/24"], driver="r8152"),
    NetworkInterface("wlan0", kind="wifi"),
    NetworkInterface("usb0", up=True, kind="usb", addresses=["172.20.1.51/24", "10.1.1.1/8"]),
]
LEASE = {"interface": "eth0", "mode": "dhcp", "addresses": ["192.168.1.77/24"], "gateway": None, "dns": None}
BY_HAND = {
    "interface": "eth0",
    "mode": "static",
    "addresses": ["10.0.5.2/16"],
    "gateway": "10.0.5.1",
    "dns": ["1.1.1.1", "8.8.8.8"],
}

FRAMEBUFFERS = [
    Framebuffer("mxc_lcd", virtual_size="1920x1080", bits_per_pixel=32, state="running"),
    Framebuffer("udlfb", virtual_size="1280x720", bits_per_pixel=16, state="suspended"),
]
SERIAL_PORTS = [
    SerialPort("/dev/ttyUSB0", driver="ftdi_sio", usb_ids="0403:6001"),
    SerialPort("/dev/ttyACM0", driver="cdc_acm", usb_ids="2341:0043"),
    SerialPort("/dev/ttyS0"),
]


class FakePage:
    def __init__(self, width=412):
        self.width, self.height = width, 915
        self.platform = ft.PagePlatform.LINUX
        self.controls, self.dialogs, self.navigation_bar, self.on_resize = [], [], None, None
        self.tasks = []

    def run_task(self, fn, *args):
        task = asyncio.ensure_future(fn(*args))
        self.tasks.append(task)
        return task

    def add(self, *controls):
        self.controls.extend(controls)

    def update(self):
        pass

    def show_dialog(self, d):
        self.dialogs.append(d)

    def pop_dialog(self):
        return self.dialogs.pop() if self.dialogs else None


def walk(control):
    stack, seen = [control], set()
    while stack:
        c = stack.pop()
        if c is None or isinstance(c, (str, int, float, bool, bytes)) or id(c) in seen:
            continue
        seen.add(id(c))
        if isinstance(c, (list, tuple)):
            stack.extend(c)
            continue
        yield c
        for attr in ("content", "controls", "actions", "title"):
            stack.append(getattr(c, attr, None))


def words(control) -> str:
    found = []
    for c in walk(control):
        for attr in ("value", "content", "tooltip"):
            if isinstance(getattr(c, attr, None), str):
                found.append(getattr(c, attr))
    return " | ".join(found)


def find(control, key):
    return next(c for c in walk(control) if getattr(c, "key", None) == key)


def keys(control) -> list:
    return [getattr(c, "key", None) for c in walk(control)]


@unittest.skipIf(ft is None, "GUI extra not installed")
class Base(unittest.IsolatedAsyncioTestCase):
    def patch(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.patch(mock.patch.dict(os.environ, {"QUAK_CONFIG_DIR": tmp.name, "QUAK_LANG": ""}))
        os.environ.pop("QUAK_CAMERA_ADDRESS", None)  # patch.dict puts it back
        for target in ("subprocess.run", "subprocess.Popen", "socket.create_connection"):
            self.patch(mock.patch(target, side_effect=AssertionError(f"{target} must not run in these tests")))
        self.manager = mock.create_autospec(SshManager, instance=True)
        self.make_manager = self.patch(mock.patch("quak.ssh.manager.SshManager", return_value=self.manager))
        self.service = mock.create_autospec(LabService, instance=True)
        self.service.status.return_value = READY
        self.service.devices.return_value = DEVICES
        self.service.storage_list.return_value = VOLUMES
        self.service.system_status.return_value = SYSTEM
        self.service.system_logs.return_value = LOGS
        self.service.audio_list.return_value = SOUND_CARDS
        self.service.audio_save.return_value = SAVED
        self.service.network_list.return_value = INTERFACES
        self.service.display_list.return_value = FRAMEBUFFERS
        self.service.serial_list.return_value = SERIAL_PORTS
        self.make_service =self.patch(mock.patch("quak.lab.service.LabService", return_value=self.service))
        self.sim = SimCamera(latency=0.0005)
        self.svc = CameraService(Target(sim=True, sim_camera=self.sim, keepalive=None), auto_reconnect=False)
        self.page = FakePage()
        self.app = gui_app.App(self.page, self.svc, sim=False)
        self.app.screen, self.app.more_tab = "more", "lab"
        await self.svc.connect()

    async def asyncTearDown(self):
        await self.svc.disconnect()
        await self.sim.close()

    @property
    def view(self):
        return screens.lab(self.app)

    def open(self, tab):
        self.app.lab["tab"] = tab

    async def settle(self):
        while pending := [task for task in self.page.tasks if not task.done()]:
            await asyncio.gather(*pending)

    async def press(self, key):
        find(self.view, key).on_click(None)
        await self.settle()

    async def answer(self, caption):
        button = next(b for b in self.page.dialogs[-1].actions if b.content == caption)
        button.on_click(None)
        await self.settle()

    async def read_port(self, port):
        self.service.status.return_value = replace(READY, host_mode=port)
        self.open("overview")
        await self.press("lab-refresh")

    async def read_everything(self):
        self.open("overview")
        await self.press("lab-refresh")
        self.open("audio")
        await self.press("lab-audio-refresh")
        self.open("storage")
        await self.press("lab-storage-refresh")
        self.open("network")
        await self.press("lab-network-refresh")
        self.open("display")
        await self.press("lab-display-refresh")
        self.open("serial")
        await self.press("lab-serial-refresh")
        self.open("system")
        await self.press("lab-system-refresh")
        await self.press("lab-logs")

    def assert_enabled(self, *names):
        for key in names:
            self.assertFalse(find(self.view, key).disabled, key)

    def assert_disabled(self, *names):
        for key in names:
            self.assertTrue(find(self.view, key).disabled, key)

    def banner_text(self, key):
        return find(self.view, key).content.controls[0].value


EMPTY_STATES = {
    "overview": ("Not checked", "Devices (hubs not counted)", "Toolkit", "Battery", "Refresh", "Enable host"),
    "usb": ("Press Rescan to list", "Rescan"),
    "audio": ("Press Refresh to list the camera's sound cards.", "Refresh"),
    "storage": ("Press Refresh to list the camera's volumes.", "Refresh"),
    "network": ("Press Refresh to list the camera's network interfaces.", "carries this SSH link", "Refresh"),
    "display": (
        "Media Mod HDMI",
        "USB DisplayLink",
        "USB-C HDMI/DP",
        "FRAMEBUFFERS",
        "Press Refresh to list the camera's framebuffers.",
        "only with a matching Linux driver",
        "VIDEO DEVICES",
        "Press Rescan to list",
    ),
    "serial": ("Press Refresh to list the camera's serial ports.", "Refresh"),
    "system": ("Press Refresh to read the camera's status.", "KERNEL LOG", "Press Read logs", "Refresh", "Read logs"),
}
ACTIONS = {
    "overview": "lab-refresh",
    "usb": "lab-rescan",
    "audio": "lab-audio-refresh",
    "storage": "lab-storage-refresh",
    "network": "lab-network-refresh",
    "display": "lab-rescan",
    "serial": "lab-serial-refresh",
    "system": "lab-system-refresh",
}


class EmptyState(Base):
    async def test_every_tab_says_what_it_needs_before_anything_is_read(self):
        self.assertEqual(sorted(EMPTY_STATES), sorted(TABS))
        for tab, expected in EMPTY_STATES.items():
            with self.subTest(tab):
                self.open(tab)
                text = words(self.view)
                for phrase in expected:
                    self.assertIn(phrase, text)
                self.assert_enabled(ACTIONS[tab])

    async def test_building_any_tab_does_nothing_on_its_own(self):
        for tab in TABS:
            self.open(tab)
            self.assertTrue(words(self.view))
        await self.settle()
        self.make_service.assert_not_called()
        self.make_manager.assert_not_called()
        self.assertEqual(self.service.method_calls, [])

    async def test_the_port_cant_be_switched_before_its_state_has_been_seen(self):
        self.assert_disabled("lab-host-on", "lab-host-off")

    async def test_the_battery_and_device_count_are_blank_until_known(self):
        self.assertEqual(words(self.view).count("—"), 3)  # devices, toolkit and battery; the camera has a name

    async def test_demo_mode_offers_nothing_on_any_tab_and_reaches_nothing(self):
        self.app.sim = True
        for tab in TABS:
            with self.subTest(tab):
                self.open(tab)
                self.assertIn("demo mode", words(self.view))
                self.assert_disabled(ACTIONS[tab])
        with self.assertRaisesRegex(LabError, "demo mode"):
            self.app.lab_service  # noqa: B018 - it says why
        self.make_service.assert_not_called()

    async def test_with_no_camera_any_tab_says_so(self):
        await self.svc.disconnect()
        for tab in TABS:
            with self.subTest(tab):
                self.open(tab)
                self.assertIn("Not connected.", words(self.view))
                self.assert_disabled(ACTIONS[tab])

    async def test_with_no_camera_connected_the_last_one_can_still_use_usb(self):
        await self.svc.disconnect()
        with mock.patch.dict(os.environ, {"QUAK_CAMERA_ADDRESS": "AA:BB:CC:DD:EE:FF"}):
            self.assertNotIn("Not connected.", words(self.view))
            await self.press("lab-refresh")
        self.make_service.assert_called_once_with(self.manager, "AA:BB:CC:DD:EE:FF")

    async def test_outside_host_mode_the_device_tabs_say_why_theres_nothing(self):
        self.service.devices.return_value = []
        self.open("usb")
        await self.press("lab-rescan")
        for tab in ("usb", "display"):
            with self.subTest(tab):
                self.open(tab)
                self.assertIn("only has USB devices in host mode", words(self.view))

    async def test_with_only_a_root_hub_each_class_says_it_found_none(self):
        self.service.devices.return_value = [ROOT_HUB]
        self.open("usb")
        await self.press("lab-rescan")
        self.open("display")
        self.assertIn("No USB video devices found.", words(self.view))
        self.open("usb")
        self.assertIn("xHCI Host Controller", words(self.view))  # the full list still has it
        self.open("overview")
        self.assertIn("| 0 |", words(self.view))  # and it isn't something plugged in

    async def test_no_volumes_says_so(self):
        self.service.storage_list.return_value = []
        self.open("storage")
        await self.press("lab-storage-refresh")
        self.assertIn("No volumes found.", words(self.view))

    async def test_a_camera_with_nothing_to_report_says_so(self):
        self.service.system_status.return_value = SYSTEM_SILENT
        self.service.system_logs.return_value = ""
        self.open("system")
        await self.press("lab-system-refresh")
        await self.press("lab-logs")
        text = words(self.view)
        self.assertIn("The camera reported nothing.", text)
        self.assertIn("The kernel log is empty.", text)


class ReadyState(Base):
    async def test_every_tab_renders_with_everything_read(self):
        await self.read_everything()
        expected = {
            "overview": ("On", "4", "Installed", "Battery"),
            "usb": ("xHCI Host Controller", "usb1", "Field Mic", "1-1", "Gigabit Adapter", "FT232R", "HD Webcam"),
            "audio": ("Field Mic", "Card 1, device 0", "Second Mic"),
            "storage": ("STICK", "DATA", "/dev/mmcblk1p1"),
            "network": ("eth0", "Ethernet", "192.168.1.5/24"),
            "display": ("Supported", "Experimental", "Not supported", "mxc_lcd", "HD Webcam"),
            "serial": ("/dev/ttyUSB0", "ftdi_sio · 0403:6001", "microcom -s 115200 /dev/ttyUSB0"),
            "system": ("Up 1:02:05", "cpu: 41.5 °C", "Field Mic"),
        }
        self.assertEqual(sorted(expected), sorted(TABS))
        for tab, phrases in expected.items():
            with self.subTest(tab):
                self.open(tab)
                text = words(self.view)
                for phrase in phrases:
                    self.assertIn(phrase, text)
                self.assertNotIn("lab-error", keys(self.view))
                self.assert_enabled(ACTIONS[tab])

    async def test_overview_reads_the_status_and_doesnt_count_hubs(self):
        await self.press("lab-refresh")
        self.service.status.assert_awaited_once_with()
        text = words(self.view)
        self.assertIn("| 4 |", text)
        self.assertNotIn("| 5 |", text)

    async def test_overview_has_a_place_for_the_battery_it_cant_read_yet(self):
        await self.press("lab-refresh")
        self.assertIn("Battery", words(self.view))
        self.service.status.return_value = replace(READY, battery="87%")
        await self.press("lab-refresh")
        self.assertIn("87%", words(self.view))

    async def test_the_toolkit_is_as_the_card_has_it(self):
        self.service.status.return_value = replace(READY, toolkit=PackState())
        await self.press("lab-refresh")
        self.assertIn("Not installed", words(self.view))

    async def test_the_usb_list_gives_class_driver_and_speed(self):
        self.open("usb")
        await self.press("lab-rescan")
        self.service.devices.assert_awaited_once_with()
        text = words(self.view)
        for row in (
            "Audio · snd-usb-audio · 12 Mb/s",
            "Network · r8152 · 5000 Mb/s",
            "Serial · ftdi_sio · 12 Mb/s",
            "Video · uvcvideo · 480 Mb/s",
            "Hub · hub · 5000 Mb/s",
        ):
            self.assertIn(row, text)

    async def test_a_device_can_say_very_little(self):
        self.service.devices.return_value = [UsbDevice(id="1-6"), THUMB]
        self.open("usb")
        await self.press("lab-rescan")
        text = words(self.view)
        self.assertIn("Other · No driver · —", text)
        self.assertIn("Storage · usb-storage · 480 Mb/s", text)

    async def test_the_display_tab_only_lists_video_devices(self):
        self.open("usb")
        await self.press("lab-rescan")
        self.open("display")
        text = words(self.view)
        self.assertIn("HD Webcam", text)
        self.assertNotIn("FT232R", text)
        self.assertNotIn("xHCI", text)

    async def test_the_display_tab_states_the_support_matrix_with_no_camera_too(self):
        await self.svc.disconnect()
        self.open("display")
        rows = [
            c
            for c in walk(self.view)
            if isinstance(c, ft.Row) and len(c.controls) == 2 and isinstance(c.controls[1], ft.Container)
        ]
        self.assertCountEqual(
            [(row.controls[0].value, row.controls[1].content.controls[0].value) for row in rows],
            [
                ("Media Mod HDMI", "Supported"),
                ("USB DisplayLink", "Experimental"),
                ("USB-C HDMI/DP", "Not supported"),
            ],
        )

    async def test_a_tab_can_be_chosen_on_a_phone_and_in_a_wide_window(self):
        for width in (412, 1400):
            self.page.width = width
            for tab in TABS:
                with self.subTest(width=width, tab=tab):
                    find(self.view, f"lab-tab-{tab}").on_click(None)
                    self.assertEqual(self.app.lab["tab"], tab)
                    self.assertEqual(find(self.view, f"lab-tab-{tab}").bgcolor, self.app.t.surface2)

    async def test_the_status_tab_reads_status_and_the_logs_are_their_own_trip(self):
        self.open("system")
        await self.press("lab-system-refresh")
        self.service.system_status.assert_awaited_once_with()
        self.service.system_logs.assert_not_awaited()
        text = words(self.view)
        for phrase in (
            "Up 1:02:05",
            "Load 0.5 0.25 0.1",
            "Memory: 500 MB available of 2.0 GB",
            "SD card: 100.0 GB free of 128.0 GB (22% used)",
        ):
            self.assertIn(phrase, text)
        self.assertIn("Press Read logs", text)
        await self.press("lab-logs")
        self.service.system_logs.assert_awaited_once_with()
        self.assertIn("Field Mic", words(self.view))

    async def test_only_one_action_runs_at_a_time(self):
        gate = asyncio.Event()

        async def slow_status():
            await gate.wait()
            return READY

        self.service.status.side_effect = slow_status
        waiting = self.view
        find(waiting, "lab-refresh").on_click(None)
        await asyncio.sleep(0.01)
        self.assertIn("Running", words(self.view))
        self.assert_disabled("lab-refresh", "lab-host-on", "lab-host-off")
        self.open("usb")
        self.assert_disabled("lab-rescan")
        self.open("overview")
        find(waiting, "lab-refresh").on_click(None)  # a tap that landed on the screen from before
        await asyncio.sleep(0.01)
        gate.set()
        await self.settle()
        self.service.status.assert_awaited_once_with()
        self.assert_enabled("lab-refresh")


class HostMode(Base):
    async def test_enable_asks_first_then_shows_what_the_camera_said(self):
        await self.read_port(PORT_OFF)
        self.assert_enabled("lab-host-on")
        self.assert_disabled("lab-host-off")
        await self.press("lab-host-on")
        self.assertIn("cuts off a computer connected to it", words(self.page.dialogs[-1].content))
        self.service.host_on.assert_not_awaited()
        self.service.host_on.return_value = HostModeResult(PORT_ON, ["host"], "Host mode is on")
        await self.answer("Enable host")
        self.service.host_on.assert_awaited_once_with()
        self.assertEqual(self.app.lab["status"].host_mode, PORT_ON)
        self.assertEqual(self.page.dialogs[-1].content.value, "Host mode is on")
        self.assert_enabled("lab-host-off")
        self.assert_disabled("lab-host-on")

    async def test_exit_asks_first_then_shows_what_the_camera_said(self):
        await self.read_port(PORT_ON)
        self.assert_enabled("lab-host-off")
        self.assert_disabled("lab-host-on")
        await self.press("lab-host-off")
        self.assertIn("back in device mode", words(self.page.dialogs[-1].content))
        self.service.host_off.assert_not_awaited()
        self.service.host_off.return_value = HostModeResult(PORT_OFF, ["gadget"], "Restored: gadget")
        await self.answer("Exit host")
        self.service.host_off.assert_awaited_once_with()
        self.assertEqual(self.app.lab["status"].host_mode, PORT_OFF)
        self.assert_enabled("lab-host-on")

    async def test_cancelling_changes_nothing(self):
        await self.read_port(PORT_OFF)
        await self.press("lab-host-on")
        await self.answer("Cancel")
        self.service.host_on.assert_not_awaited()
        self.assertEqual(self.page.dialogs, [])

    async def test_a_switch_keeps_the_toolkit_but_drops_what_it_made_out_of_date(self):
        await self.read_everything()
        self.service.host_off.return_value = HostModeResult(PORT_OFF, [], "Nothing needed restoring")
        self.open("overview")
        await self.press("lab-host-off")
        calls = self.service.status.await_count
        await self.answer("Exit host")
        self.assertEqual(self.service.status.await_count, calls)  # its answer already has the port's state
        self.assertIn("Installed", words(self.view))
        self.assertIsNone(self.app.lab["devices"])
        self.assertIsNone(self.app.lab["volumes"])
        self.assertIsNone(self.app.lab["audio"])
        self.assertIsNone(self.app.lab["network"])
        self.assertIsNone(self.app.lab["framebuffers"])
        self.assertIsNone(self.app.lab["serial"])
        self.open("usb")
        self.assertIn("Press Rescan", words(self.view))
        self.open("storage")
        self.assertIn("Press Refresh", words(self.view))
        self.open("network")
        self.assertIn("Press Refresh", words(self.view))
        self.open("display")
        self.assertIn("Press Refresh to list the camera's framebuffers.", words(self.view))
        self.open("serial")
        self.assertIn("Press Refresh to list the camera's serial ports.", words(self.view))

    async def test_a_port_between_modes_can_only_go_back(self):
        await self.read_port(PORT_BETWEEN)
        self.assertIn("Between modes", words(self.view))
        self.assert_enabled("lab-host-off")
        self.assert_disabled("lab-host-on")

    async def test_a_switch_that_didnt_finish_says_how_to_recover(self):
        await self.read_port(PORT_STUCK)
        self.assertIn("quak lab host recover", words(self.view))

    async def test_a_camera_without_the_switch_says_so_and_offers_nothing(self):
        await self.read_port(NO_SWITCH)
        self.assertIn("Unavailable", words(self.view))
        self.assertEqual(self.banner_text("lab-port-error"), NO_SWITCH.error)
        self.assert_disabled("lab-host-on", "lab-host-off")


class Storage(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.open("storage")
        await self.press("lab-storage-refresh")

    async def test_each_volume_says_what_it_is_and_where_it_is(self):
        text = words(self.view)
        for phrase in (
            "/dev/sda1 · vfat · 8.0 GB",
            "Not mounted",
            "/dev/sdb1 · exfat · 64.0 GB",
            "Mounted at /tmp/quak/mnt/sdb1 (read-only)",
            "/dev/mmcblk1p1 · Unknown filesystem · 128.0 GB",
            "Mounted at /tmp/fuse_d (read-write)",
        ):
            self.assertIn(phrase, text)

    async def test_mounting_is_always_read_only_and_then_lists_again(self):
        await self.press("lab-mount-/dev/sda1")
        self.service.storage_mount.assert_awaited_once_with("/dev/sda1", read_only=True)
        self.assertEqual(self.service.storage_list.await_count, 2)

    async def test_unmounting_uses_the_mountpoint_and_then_lists_again(self):
        await self.press("lab-unmount-/dev/sdb1")
        self.service.storage_unmount.assert_awaited_once_with("/tmp/quak/mnt/sdb1")
        self.assertEqual(self.service.storage_list.await_count, 2)

    async def test_only_the_labs_own_mounts_can_be_unmounted(self):
        found = keys(self.view)
        self.assertIn("lab-mount-/dev/sda1", found)
        self.assertNotIn("lab-unmount-/dev/sda1", found)
        self.assertIn("lab-unmount-/dev/sdb1", found)
        self.assertNotIn("lab-mount-/dev/sdb1", found)
        self.assertNotIn("lab-unmount-/dev/mmcblk1p1", found)  # the SD card stays mounted
        self.assertNotIn("lab-mount-/dev/mmcblk1p1", found)

    async def test_a_refused_mount_is_an_error_and_the_list_stays(self):
        self.service.storage_mount.side_effect = LabError("could not mount /dev/sda1: busy")
        await self.press("lab-mount-/dev/sda1")
        self.assertEqual(self.banner_text("lab-error"), "could not mount /dev/sda1: busy")
        self.assertEqual(self.service.storage_list.await_count, 1)
        self.assertIn("STICK", words(self.view))

    async def test_a_disk_in_use_stays_mounted_and_says_so(self):
        self.service.storage_unmount.side_effect = LabError("target is busy")
        await self.press("lab-unmount-/dev/sdb1")
        self.assertEqual(self.banner_text("lab-error"), "target is busy")
        self.assertIn("Mounted at /tmp/quak/mnt/sdb1 (read-only)", words(self.view))


class Network(Base):
    REFUSED = {
        "address": ("", "abc", "10.0.0.256", "10.0.0.2/24", "127.0.0.1", "0.0.0.0", "224.0.0.1", "fe80::1"),
        "prefix": ("", "abc", "0", "33", "-1", "24.0", "2_4", "٢٤"),
        "gateway": ("abc", "10.9.0.1", "10.0.0.2"),
        "dns": ("abc", ",", "1.1.1.1,", "1.1.1.1,nope"),
    }

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.service.network_configure.return_value = LEASE
        self.open("network")
        await self.press("lab-network-refresh")

    def field(self, name):
        return find(self.page.dialogs[-1], f"lab-static-{name}")

    def row(self, name) -> set[str]:
        return set(words(find(self.view, f"lab-network-row-{name}")).split(" | "))

    async def type_static(self, **typed):
        for name, value in typed.items():
            self.field(name).value = value
        await self.answer("Apply")

    async def use_dhcp(self):
        await self.press("lab-network-dhcp-eth0")
        await self.answer("Use DHCP")

    # the list

    async def test_refresh_lists_each_interface_with_its_kind_state_and_addresses(self):
        self.service.network_list.assert_awaited_once_with()
        expected = {
            "lo": ("Other", "Up", "127.0.0.1/8"),
            "eth0": ("Ethernet", "Up", "192.168.1.5/24"),
            "wlan0": ("Wi-Fi", "Down", "No address"),
            "usb0": ("USB", "Up", "172.20.1.51/24, 10.1.1.1/8"),
        }
        for name, facts in expected.items():
            with self.subTest(name):
                self.assertLessEqual({name, *facts}, self.row(name))

    async def test_a_kind_it_has_no_word_for_is_other(self):
        self.service.network_list.return_value = [NetworkInterface("tun0", kind="tunnel")]
        await self.press("lab-network-refresh")
        self.assertIn("Other", self.row("tun0"))

    async def test_every_interface_but_loopback_can_be_changed(self):
        found = keys(self.view)
        for name in ("eth0", "wlan0", "usb0"):
            self.assertIn(f"lab-network-dhcp-{name}", found)
            self.assertIn(f"lab-network-static-{name}", found)
        self.assertNotIn("lab-network-dhcp-lo", found)
        self.assertNotIn("lab-network-static-lo", found)

    async def test_no_interfaces_says_so(self):
        self.service.network_list.return_value = []
        await self.press("lab-network-refresh")
        self.assertIn("No network interfaces found.", words(self.view))

    async def test_listing_changes_nothing(self):
        self.service.network_configure.assert_not_awaited()

    async def test_the_old_class_list_is_gone_and_the_usb_tab_still_has_the_adapter(self):
        self.assertNotIn("lab-rescan", keys(self.view))
        self.open("usb")
        await self.press("lab-rescan")
        self.assertIn("Network · r8152 · 5000 Mb/s", words(self.view))

    async def test_the_panel_warns_about_the_link_with_the_list_on_screen(self):
        self.assertIn("carries this SSH link can drop", self.banner_text("lab-network-warning"))

    # DHCP

    async def test_dhcp_asks_first_warns_about_the_link_then_shows_the_address(self):
        await self.press("lab-network-dhcp-eth0")
        self.assertEqual(self.page.dialogs[-1].title.value, "Use DHCP on eth0?")
        self.assertIn("this SSH link runs over eth0", words(self.page.dialogs[-1].content))
        self.service.network_configure.assert_not_awaited()
        await self.answer("Use DHCP")
        self.service.network_configure.assert_awaited_once_with("eth0", dhcp=True)
        self.assertEqual(self.page.dialogs, [])
        self.assertIn("eth0 set up by DHCP: 192.168.1.77/24", self.banner_text("lab-network-result"))
        self.assertIn("lasts until the camera restarts", self.banner_text("lab-network-result"))

    async def test_cancelling_dhcp_changes_nothing(self):
        await self.press("lab-network-dhcp-eth0")
        await self.answer("Cancel")
        self.service.network_configure.assert_not_awaited()
        self.assertEqual(self.page.dialogs, [])
        self.assertNotIn("lab-network-result", keys(self.view))

    async def test_the_answer_updates_its_row_without_another_trip(self):
        self.service.network_configure.return_value = {**LEASE, "interface": "wlan0"}
        await self.press("lab-network-dhcp-wlan0")
        await self.answer("Use DHCP")
        self.service.network_configure.assert_awaited_once_with("wlan0", dhcp=True)
        self.assertLessEqual({"Up", "192.168.1.77/24"}, self.row("wlan0"))
        self.assertNotIn("Down", self.row("wlan0"))
        self.assertIn("192.168.1.5/24", self.row("eth0"))  # the others are as they were
        self.assertEqual(self.service.network_list.await_count, 1)
        self.assertEqual(INTERFACES[2].addresses, [])  # and what was read isn't written over in place

    async def test_reading_the_list_again_drops_the_last_result(self):
        await self.use_dhcp()
        await self.press("lab-network-refresh")
        self.assertNotIn("lab-network-result", keys(self.view))

    # static

    async def test_static_opens_with_the_default_prefix_and_warns_before_anything_changes(self):
        await self.press("lab-network-static-eth0")
        self.assertEqual(self.page.dialogs[-1].title.value, "Static address on eth0")
        self.assertEqual(self.field("prefix").value, "24")
        self.assertIn("this SSH link runs over eth0", words(self.page.dialogs[-1].content))
        self.service.network_configure.assert_not_awaited()
        await self.answer("Cancel")
        self.assertEqual(self.page.dialogs, [])
        self.service.network_configure.assert_not_awaited()

    async def test_static_sends_what_was_typed_and_shows_the_result(self):
        self.service.network_configure.return_value = BY_HAND
        await self.press("lab-network-static-eth0")
        await self.type_static(address=" 10.0.5.2 ", prefix=" 16 ", gateway=" 10.0.5.1 ", dns=" 1.1.1.1 , 8.8.8.8 ")
        self.service.network_configure.assert_awaited_once_with(
            "eth0", dhcp=False, address="10.0.5.2", prefix=16, gateway="10.0.5.1", dns=["1.1.1.1", "8.8.8.8"]
        )
        self.assertEqual(self.page.dialogs, [])
        result = self.banner_text("lab-network-result")
        for phrase in ("eth0 set up by hand: 10.0.5.2/16", "Gateway 10.0.5.1", "DNS 1.1.1.1, 8.8.8.8"):
            self.assertIn(phrase, result)
        self.assertIn("10.0.5.2/16", self.row("eth0"))
        self.assertEqual(self.service.network_list.await_count, 1)

    async def test_static_leaves_out_what_was_left_blank(self):
        await self.press("lab-network-static-eth0")
        await self.type_static(address="10.0.0.2")
        self.service.network_configure.assert_awaited_once_with(
            "eth0", dhcp=False, address="10.0.0.2", prefix=24, gateway=None, dns=None
        )

    async def test_static_takes_ipv6_dns_servers_as_the_backend_does(self):
        await self.press("lab-network-static-eth0")
        await self.type_static(address="10.0.0.2", dns="2606:4700:4700::1111")
        self.assertEqual(self.service.network_configure.await_args.kwargs["dns"], ["2606:4700:4700::1111"])

    async def test_static_keeps_the_dialog_open_and_marks_the_field_for_each_bad_value(self):
        good = {"address": "10.0.0.2", "prefix": "24", "gateway": "", "dns": ""}
        await self.press("lab-network-static-eth0")
        for name, values in self.REFUSED.items():
            for value in values:
                with self.subTest(field=name, typed=value):
                    await self.type_static(**{**good, name: value})
                    self.assertEqual([field for field in good if self.field(field).error], [name])
                    self.assertIsInstance(self.page.dialogs[-1], ft.AlertDialog)
                    self.service.network_configure.assert_not_awaited()

    async def test_the_gateway_is_checked_against_the_prefix_typed(self):
        await self.press("lab-network-static-eth0")
        await self.type_static(address="10.0.0.2", prefix="24", gateway="10.0.5.1")
        self.assertTrue(self.field("gateway").error)
        await self.type_static(prefix="16")
        self.service.network_configure.assert_awaited_once_with(
            "eth0", dhcp=False, address="10.0.0.2", prefix=16, gateway="10.0.5.1", dns=None
        )

    async def test_a_field_put_right_loses_its_mark(self):
        await self.press("lab-network-static-eth0")
        await self.type_static(address="abc", prefix="99")
        self.assertTrue(self.field("address").error)
        self.assertTrue(self.field("prefix").error)
        await self.type_static(prefix="24")
        self.assertTrue(self.field("address").error)
        self.assertFalse(self.field("prefix").error)
        await self.type_static(address="10.0.0.2")
        self.service.network_configure.assert_awaited_once()

    async def test_enter_cant_send_a_half_filled_form(self):
        await self.press("lab-network-static-eth0")
        for name in ("address", "prefix", "gateway", "dns"):
            self.assertIsNone(self.field(name).on_submit, name)

    # failures

    async def test_a_failure_is_shown_once_and_the_list_goes_until_it_is_read_again(self):
        self.service.network_configure.side_effect = LabError("eth0 now has no address")
        await self.press("lab-network-static-eth0")
        await self.type_static(address="10.0.0.2")
        self.assertEqual(self.banner_text("lab-error"), "eth0 now has no address")
        self.assertEqual(self.page.dialogs[-1].content.value, "eth0 now has no address")  # App.run's toast
        self.assertIsInstance(self.page.dialogs[-1], ft.SnackBar)
        self.assertNotIn("lab-network-row-eth0", keys(self.view))  # what it showed may no longer be so
        self.assertIn("Press Refresh", words(self.view))
        self.assertFalse(self.app.lab["busy"])
        self.assert_enabled("lab-network-refresh")
        self.service.network_configure.assert_awaited_once()
        self.assertEqual(self.service.network_list.await_count, 1)

    async def test_a_lost_link_says_the_change_may_have_gone_through_and_isnt_tried_again(self):
        self.service.network_configure.side_effect = NetworkError("ssh lost the connection to the camera")
        await self.use_dhcp()
        await self.settle()
        message = self.banner_text("lab-error")
        for phrase in ("ssh lost the connection", "runs over eth0", "may have gone through anyway", "press Refresh"):
            self.assertIn(phrase, message)
        self.assertEqual(self.page.dialogs[-1].content.value, message)
        self.service.network_configure.assert_awaited_once_with("eth0", dhcp=True)
        self.assertEqual(self.service.network_list.await_count, 1)
        self.assertNotIn("lab-network-row-eth0", keys(self.view))

    async def test_a_failure_takes_the_last_result_off_the_screen(self):
        await self.use_dhcp()
        self.service.network_configure.side_effect = LabError("no DHCP lease on eth0")
        await self.use_dhcp()
        self.assertEqual(self.banner_text("lab-error"), "no DHCP lease on eth0")
        self.assertNotIn("lab-network-result", keys(self.view))

    async def test_taking_the_adapter_is_a_question_and_only_a_yes_runs_it_again(self):
        self.service.network_configure.side_effect = [AdapterInUse("wlan0"), LEASE]
        await self.use_dhcp()
        self.service.network_configure.assert_awaited_once_with("eth0", dhcp=True)
        self.assertEqual(self.app.lab["error"], "")
        self.assertIn("wlan0", self.page.dialogs[-1].title.value)
        self.assertIn("eth0", self.row("eth0"))  # nothing reached the camera, so the list still holds
        await self.answer("Use it")
        self.assertEqual(self.service.network_configure.await_count, 2)
        self.assertIn("192.168.1.77/24", self.banner_text("lab-network-result"))

    async def test_saying_no_to_the_adapter_runs_nothing_again(self):
        self.service.network_configure.side_effect = [AdapterInUse("wlan0"), LEASE]
        await self.use_dhcp()
        await self.answer("Cancel")
        self.service.network_configure.assert_awaited_once()

    # when it can't run

    async def test_with_no_camera_or_in_demo_mode_nothing_can_be_changed(self):
        await self.svc.disconnect()
        self.app.lab.update(address=None, network=INTERFACES)  # a list left on screen doesn't make it possible
        self.assert_disabled("lab-network-refresh", "lab-network-dhcp-eth0", "lab-network-static-eth0")
        self.assertIn("Not connected.", words(self.view))
        self.app.sim = True
        self.assert_disabled("lab-network-refresh", "lab-network-dhcp-eth0", "lab-network-static-eth0")
        self.assertIn("demo mode", words(self.view))

    async def test_a_camera_lost_while_the_dialog_was_open_is_reported_and_not_touched(self):
        await self.press("lab-network-dhcp-eth0")
        await self.svc.disconnect()
        await self.answer("Use DHCP")
        self.service.network_configure.assert_not_awaited()
        self.assertEqual(self.page.dialogs[-1].content.value, "Not connected.")
        self.assertFalse(self.app.lab["busy"])

    async def test_another_camera_in_hand_is_not_given_the_first_ones_setup(self):
        await self.press("lab-network-static-eth0")
        self.app.lab["address"] = "OTHER"  # the list was read from that one
        await self.type_static(address="10.0.0.2")
        self.service.network_configure.assert_not_awaited()
        self.assertIn("isn't on this camera's list", self.banner_text("lab-error"))
        self.assertIn("Press Refresh", words(self.view))

    async def test_nothing_else_can_start_while_it_runs_and_the_screen_says_so(self):
        gate = asyncio.Event()

        async def slow(name, **settings):
            await gate.wait()
            return LEASE

        self.service.network_configure.side_effect = slow
        buttons = ("lab-network-refresh", "lab-network-dhcp-eth0", "lab-network-static-eth0")
        await self.press("lab-network-dhcp-eth0")
        next(b for b in self.page.dialogs[-1].actions if b.content == "Use DHCP").on_click(None)
        await asyncio.sleep(0.01)
        self.assertIn("Running", words(self.view))
        self.assert_disabled(*buttons)
        gate.set()
        await self.settle()
        self.assertNotIn("Running", words(self.view))
        self.assert_enabled(*buttons)

    async def test_it_waits_for_a_running_ssh_action(self):
        self.app.ssh["busy"] = True
        self.assert_disabled("lab-network-refresh", "lab-network-dhcp-eth0", "lab-network-static-eth0")

    async def test_what_was_found_on_another_camera_isnt_shown(self):
        await self.use_dhcp()
        self.app.lab["address"] = "OTHER"
        found = keys(self.view)
        self.assertNotIn("lab-network-row-eth0", found)
        self.assertNotIn("lab-network-result", found)


class ErrorState(Base):
    FAILING = {
        "overview": "status",
        "usb": "devices",
        "audio": "audio_list",
        "storage": "storage_list",
        "network": "network_list",
        "display": "devices",
        "serial": "serial_list",
        "system": "system_status",
    }

    async def test_every_tab_shows_its_action_failing_and_can_try_again(self):
        self.assertEqual(sorted(self.FAILING), sorted(TABS))
        for tab, method in self.FAILING.items():
            with self.subTest(tab):
                getattr(self.service, method).side_effect = LabError(f"{tab} failed")
                self.open(tab)
                await self.press(ACTIONS[tab])
                self.assertEqual(self.banner_text("lab-error"), f"{tab} failed")
                self.assertEqual(self.page.dialogs[-1].content.value, f"{tab} failed")  # App.run's toast
                self.assertIsInstance(self.page.dialogs[-1], ft.SnackBar)
                self.assertFalse(self.app.lab["busy"])
                self.assert_enabled(ACTIONS[tab])
                getattr(self.service, method).side_effect = None
                await self.press(ACTIONS[tab])
                self.assertNotIn("lab-error", keys(self.view))

    async def test_every_tab_shows_an_error_from_before(self):
        self.app.lab.update(address=ADDRESS, error="the camera went away")
        for tab in TABS:
            with self.subTest(tab):
                self.open(tab)
                self.assertEqual(self.banner_text("lab-error"), "the camera went away")

    async def test_logs_that_cant_be_read_are_an_error(self):
        self.service.system_logs.side_effect = LabError("could not read the camera's kernel log")
        self.open("system")
        await self.press("lab-logs")
        self.assertEqual(self.banner_text("lab-error"), "could not read the camera's kernel log")

    async def test_the_rest_of_the_status_survives_a_device_list_that_failed(self):
        self.service.status.return_value = replace(READY, devices=[], error="could not list the camera's USB devices")
        await self.press("lab-refresh")
        self.assertEqual(self.banner_text("lab-devices-error"), "could not list the camera's USB devices")
        self.assertIn("Installed", words(self.view))
        self.assertIsNone(self.app.lab["devices"])  # it isn't known to be empty
        self.open("usb")
        self.assertNotIn("only has USB devices in host mode", words(self.view))

    async def test_a_toolkit_problem_is_shown_with_its_reason(self):
        self.service.status.return_value = replace(READY, toolkit=PackState(error="the state file is corrupt"))
        await self.press("lab-refresh")
        self.assertIn("Problem", words(self.view))
        self.assertEqual(self.banner_text("lab-toolkit-error"), "the state file is corrupt")

    async def test_a_refused_switch_is_an_error_and_the_port_stays_as_it_was(self):
        await self.read_port(PORT_OFF)
        self.service.host_on.side_effect = HostModeError("this connection runs through the camera's USB port")
        await self.press("lab-host-on")
        await self.answer("Enable host")
        self.assertEqual(self.banner_text("lab-error"), "this connection runs through the camera's USB port")
        self.assertEqual(self.app.lab["status"].host_mode, PORT_OFF)
        self.assert_enabled("lab-host-on")

    async def test_taking_the_adapter_is_a_question_and_then_a_retry(self):
        self.service.status.side_effect = [AdapterInUse("wlan0"), READY]
        await self.press("lab-refresh")
        self.assertEqual(self.app.lab["error"], "")
        self.assertFalse(self.app.lab["busy"])
        self.assertIn("wlan0", self.page.dialogs[-1].title.value)
        await self.answer("Use it")
        self.assertTrue(self.svc.allow_busy_adapter)
        self.assertEqual(self.service.status.await_count, 2)
        self.assertIn("Installed", words(self.view))

    async def test_what_was_found_about_another_camera_is_not_shown(self):
        await self.read_everything()
        self.app.lab["error"] = "an old failure"
        self.app.lab["address"] = "OTHER"
        self.open("overview")
        text = words(self.view)
        self.assertIn("Not checked", text)
        self.assertNotIn("Installed", text)
        self.assertNotIn("lab-error", keys(self.view))
        self.assert_disabled("lab-host-on", "lab-host-off")
        self.open("usb")
        self.assertNotIn("Field Mic", words(self.view))

    async def test_the_next_action_starts_clean_on_another_camera(self):
        await self.read_everything()
        self.app.lab["address"] = "OTHER"
        self.open("usb")
        await self.press("lab-rescan")
        self.assertEqual(self.app.lab["address"], ADDRESS)
        self.assertIsNone(self.app.lab["volumes"])
        self.assertIsNone(self.app.lab["status"])
        self.assertIsNone(self.app.lab["logs"])
        self.assertIsNone(self.app.lab["audio"])
        self.assertIsNone(self.app.lab["network"])
        self.assertIsNone(self.app.lab["framebuffers"])
        self.assertIsNone(self.app.lab["serial"])

    async def test_the_port_picked_on_one_camera_isnt_picked_on_another(self):
        self.open("serial")
        await self.press("lab-serial-refresh")
        await self.press("lab-serial-/dev/ttyS0")
        self.app.lab["address"] = "OTHER"
        await self.press("lab-serial-refresh")
        self.assertIsNone(self.app.lab["serial_port"])
        self.assertEqual(find(self.view, "lab-serial-command").value, "microcom -s 115200 /dev/ttyUSB0")


class Display(Base):
    NOTE = "A framebuffer can be written only with a matching Linux driver."

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.open("display")

    async def test_refresh_lists_each_framebuffer_with_its_size_depth_and_state(self):
        await self.press("lab-display-refresh")
        self.service.display_list.assert_awaited_once_with()
        text = words(self.view)
        for phrase in ("mxc_lcd", "1920x1080 · 32 bpp · Running", "udlfb", "1280x720 · 16 bpp · Suspended"):
            self.assertIn(phrase, text)

    async def test_what_a_driver_doesnt_say_is_blank_and_an_unknown_state_is_shown_as_it_is(self):
        self.service.display_list.return_value = [Framebuffer(""), Framebuffer("odd", state="7")]
        await self.press("lab-display-refresh")
        text = words(self.view)
        self.assertIn("— · — · —", text)
        self.assertIn("— · — · 7", text)

    async def test_no_framebuffers_says_so(self):
        self.service.display_list.return_value = []
        await self.press("lab-display-refresh")
        self.assertIn("No framebuffers found.", words(self.view))

    async def test_the_note_and_the_matrix_stay_whatever_was_read(self):
        for found in (FRAMEBUFFERS, []):
            with self.subTest(found=found):
                self.service.display_list.return_value = found
                await self.press("lab-display-refresh")
                text = words(self.view)
                for phrase in (self.NOTE, "Media Mod HDMI", "USB DisplayLink", "USB-C HDMI/DP", "Not supported"):
                    self.assertIn(phrase, text)

    async def test_framebuffers_and_video_devices_are_a_trip_each(self):
        await self.press("lab-display-refresh")
        self.service.devices.assert_not_awaited()
        self.assertIn("Press Rescan to list", words(self.view))
        await self.press("lab-rescan")
        self.service.display_list.assert_awaited_once_with()
        text = words(self.view)
        self.assertIn("mxc_lcd", text)
        self.assertIn("HD Webcam", text)

    async def test_a_failure_says_so_and_can_be_tried_again(self):
        self.service.display_list.side_effect = LabError("could not list the camera's framebuffers")
        await self.press("lab-display-refresh")
        self.assertEqual(self.banner_text("lab-error"), "could not list the camera's framebuffers")
        self.assertIsNone(self.app.lab["framebuffers"])
        self.assert_enabled("lab-display-refresh")
        self.service.display_list.side_effect = None
        await self.press("lab-display-refresh")
        self.assertNotIn("lab-error", keys(self.view))
        self.assertIn("mxc_lcd", words(self.view))

    async def test_what_was_found_on_another_camera_isnt_shown(self):
        await self.press("lab-display-refresh")
        self.app.lab["address"] = "OTHER"
        self.assertNotIn("mxc_lcd", words(self.view))

    async def test_with_no_camera_or_in_demo_mode_it_cant_be_read(self):
        await self.svc.disconnect()
        self.assert_disabled("lab-display-refresh")
        self.app.sim = True
        self.assert_disabled("lab-display-refresh")


class Serial(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.open("serial")
        await self.press("lab-serial-refresh")

    @property
    def command(self):
        return find(self.view, "lab-serial-command").value

    async def test_refresh_lists_each_port_with_its_driver_and_usb_ids(self):
        self.service.serial_list.assert_awaited_once_with()
        text = words(self.view)
        for phrase in (
            "/dev/ttyUSB0",
            "ftdi_sio · 0403:6001",
            "/dev/ttyACM0",
            "cdc_acm · 2341:0043",
            "/dev/ttyS0",
            "No driver · Not on USB",
        ):
            self.assertIn(phrase, text)

    async def test_the_hint_starts_on_the_first_port_with_a_command_to_copy(self):
        text = words(self.view)
        self.assertIn("USE IN TERMINAL", text)
        self.assertIn("won't run it for you", text)
        self.assertEqual(self.command, "microcom -s 115200 /dev/ttyUSB0")
        self.assertTrue(find(self.view, "lab-serial-command").selectable)
        self.assertEqual(find(self.view, "lab-serial-/dev/ttyUSB0").bgcolor, self.app.t.surface2)

    async def test_tapping_a_port_moves_the_command_to_it_and_reaches_nothing(self):
        before = list(self.service.method_calls)
        before_manager = list(self.manager.method_calls)  # the refresh that listed the ports read the login's profile
        await self.press("lab-serial-/dev/ttyS0")
        self.assertEqual(self.command, "microcom -s 115200 /dev/ttyS0")
        self.assertEqual(find(self.view, "lab-serial-/dev/ttyS0").bgcolor, self.app.t.surface2)
        self.assertIsNone(find(self.view, "lab-serial-/dev/ttyUSB0").bgcolor)
        self.assertEqual(self.service.method_calls, before)
        self.assertEqual(self.manager.method_calls, before_manager)

    async def test_nothing_on_the_tab_can_run_the_command(self):
        pressable = [
            c.key
            for c in walk(self.view)
            if getattr(c, "on_click", None) and not str(getattr(c, "key", "")).startswith("lab-tab")
        ]
        self.assertCountEqual(
            pressable,
            ["lab-serial-/dev/ttyUSB0", "lab-serial-/dev/ttyACM0", "lab-serial-/dev/ttyS0", "lab-serial-refresh"],
        )
        for port in ("/dev/ttyUSB0", "/dev/ttyACM0", "/dev/ttyS0"):
            await self.press(f"lab-serial-{port}")
        self.assertEqual(self.service.method_calls, [mock.call.serial_list()])

    async def test_the_choice_lasts_as_long_as_the_port(self):
        await self.press("lab-serial-/dev/ttyACM0")
        await self.press("lab-serial-refresh")
        self.assertEqual(self.command, "microcom -s 115200 /dev/ttyACM0")
        self.service.serial_list.return_value = [SerialPort("/dev/ttyS0")]
        await self.press("lab-serial-refresh")
        self.assertEqual(self.command, "microcom -s 115200 /dev/ttyS0")

    async def test_no_ports_says_what_they_need_and_offers_no_command(self):
        self.service.serial_list.return_value = []
        await self.press("lab-serial-refresh")
        text = words(self.view)
        self.assertIn("No serial ports found.", text)
        self.assertIn("host mode", text)
        self.assertNotIn("USE IN TERMINAL", text)
        self.assertNotIn("lab-serial-command", keys(self.view))

    async def test_before_anything_is_read_there_is_no_command(self):
        self.app.lab["serial"] = None
        self.assertNotIn("lab-serial-command", keys(self.view))

    async def test_a_failure_says_so_and_can_be_tried_again(self):
        self.service.serial_list.side_effect = LabError("could not list the camera's serial ports")
        await self.press("lab-serial-refresh")
        self.assertEqual(self.banner_text("lab-error"), "could not list the camera's serial ports")
        self.assert_enabled("lab-serial-refresh")

    async def test_the_old_class_list_is_gone_and_the_usb_tab_still_has_the_adapter(self):
        self.assertNotIn("lab-rescan", keys(self.view))
        self.assertNotIn("can't use them yet", words(self.view))
        self.open("usb")
        await self.press("lab-rescan")
        self.assertIn("Serial · ftdi_sio · 12 Mb/s", words(self.view))

    async def test_what_was_found_on_another_camera_isnt_shown(self):
        self.app.lab["address"] = "OTHER"
        found = keys(self.view)
        self.assertNotIn("lab-serial-/dev/ttyUSB0", found)
        self.assertNotIn("lab-serial-command", found)

    async def test_with_no_camera_or_in_demo_mode_it_cant_be_read(self):
        await self.svc.disconnect()
        self.assert_disabled("lab-serial-refresh")
        self.app.sim = True
        self.assert_disabled("lab-serial-refresh")


class Audio(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.shutter = self.patch(mock.patch.object(self.svc, "shutter", mock.AsyncMock()))
        self.open("audio")
        await self.press("lab-audio-refresh")

    @property
    def field(self):
        return self.page.dialogs[-1].content

    async def record(self, key, seconds="30"):
        await self.press(key)
        self.field.value = seconds
        await self.answer("Record")

    def watch(self, capture=SAVED) -> list[str]:
        """What happens, in order. ``capture`` is what the mic's recording gives, or raises."""
        events: list[str] = []

        def save(card, seconds):
            events.append("mic")
            if isinstance(capture, Exception):
                raise capture
            return capture

        self.shutter.side_effect = lambda on: events.append("camera on" if on else "camera off")
        self.service.audio_save.side_effect = save
        return events

    def assert_nothing_recorded(self):
        self.shutter.assert_not_awaited()
        self.service.audio_save.assert_not_awaited()

    # the card list

    async def test_refresh_lists_each_card_once_with_its_device_name_and_formats(self):
        self.service.audio_list.assert_awaited_once_with()
        text = words(self.view)
        self.assertIn("Field Mic", text)
        self.assertIn("Card 1, device 0 · S16_LE, S24_3LE", text)
        self.assertIn("Card 3, device 0 · S16_LE", text)
        found = keys(self.view)
        for key in ("lab-audio-mic-1", "lab-audio-camera-1", "lab-audio-mic-3", "lab-audio-camera-3"):
            self.assertEqual(found.count(key), 1, key)

    async def test_a_card_that_cant_capture_isnt_listed(self):
        self.assertNotIn("USB Speaker", words(self.view))
        self.assertNotIn("lab-audio-mic-2", keys(self.view))

    async def test_with_no_card_that_can_capture_it_says_what_they_need(self):
        for found in ([], [SPEAKER]):
            with self.subTest(found=found):
                self.service.audio_list.return_value = found
                await self.press("lab-audio-refresh")
                text = words(self.view)
                self.assertIn("No USB audio cards that can record", text)
                self.assertIn("quak lab toolkit load", text)
                self.assertNotIn("Record mic", text)

    async def test_a_host_mode_switch_drops_the_list_of_cards_but_not_the_saved_file(self):
        await self.record("lab-audio-mic-1")
        self.service.host_off.return_value = HostModeResult(PORT_OFF, [], "")
        self.app.lab["status"] = READY
        self.open("overview")
        await self.press("lab-host-off")
        await self.answer("Exit host")
        self.open("audio")
        self.assertIn("Press Refresh to list the camera's sound cards.", words(self.view))
        self.assertIn(SAVED["path"], words(self.view))

    async def test_what_was_found_on_another_camera_isnt_shown(self):
        await self.record("lab-audio-mic-1")
        self.app.lab["address"] = "OTHER"
        text = words(self.view)
        self.assertNotIn("Field Mic", text)
        self.assertNotIn(SAVED["path"], text)
        self.assertNotIn("lab-audio-mic-1", keys(self.view))

    # the duration dialog

    async def test_both_buttons_ask_how_long_and_record_nothing_until_answered(self):
        for key, title in (("lab-audio-mic-1", "Record mic"), ("lab-audio-camera-1", "Record with camera")):
            with self.subTest(key):
                await self.press(key)
                self.assertEqual(self.page.dialogs[-1].title.value, title)
                self.assertEqual(self.field.value, "30")
                self.assertIn("300", self.field.label)
                self.assert_nothing_recorded()
                await self.answer("Cancel")
                self.assertEqual(self.page.dialogs, [])
                self.assert_nothing_recorded()

    async def test_the_dialog_only_takes_a_length_a_capture_can_have(self):
        await self.press("lab-audio-mic-1")
        for typed in ("", "abc", "0", "-5", "1.5", "301"):
            with self.subTest(typed=typed):
                self.field.value = typed
                await self.answer("Record")
                self.assertIn("300", self.field.error)
                self.assertIsInstance(self.page.dialogs[-1], ft.AlertDialog)  # still open, to try again
                self.assert_nothing_recorded()
        self.field.value = "300"
        await self.answer("Record")
        self.service.audio_save.assert_awaited_once_with(1, 300)
        self.assertEqual(self.page.dialogs, [])

    async def test_enter_answers_the_dialog_too(self):
        await self.press("lab-audio-mic-3")
        self.field.value = "45"
        self.field.on_submit(None)
        await self.settle()
        self.service.audio_save.assert_awaited_once_with(3, 45)

    # record mic

    async def test_record_mic_saves_from_that_card_and_shows_where_without_the_camera(self):
        await self.record("lab-audio-mic-3", "12")
        self.service.audio_save.assert_awaited_once_with(3, 12)
        self.shutter.assert_not_awaited()
        self.assertEqual(self.banner_text("lab-audio-saved"), f"Saved {SAVED['path']} (5.8 MB)")
        self.assertFalse(self.app.lab["busy"])

    async def test_a_capture_that_fails_says_so_and_takes_the_last_file_off_the_screen(self):
        await self.record("lab-audio-mic-1")
        self.service.audio_save.side_effect = LabError("tinycap wrote no audio")
        await self.record("lab-audio-mic-1")
        self.assertEqual(self.banner_text("lab-error"), "tinycap wrote no audio")
        self.assertNotIn("lab-audio-saved", keys(self.view))
        self.assert_enabled("lab-audio-mic-1")

    async def test_nothing_else_can_start_while_it_records(self):
        gate = asyncio.Event()

        async def slow_save(card, seconds):
            await gate.wait()
            return SAVED

        self.service.audio_save.side_effect = slow_save
        await self.press("lab-audio-mic-1")
        next(b for b in self.page.dialogs[-1].actions if b.content == "Record").on_click(None)
        await asyncio.sleep(0.01)
        self.assertIn("Running", words(self.view))
        self.assert_disabled("lab-audio-refresh", "lab-audio-mic-1", "lab-audio-camera-1")
        gate.set()
        await self.settle()
        self.assertNotIn("Running", words(self.view))
        self.assert_enabled("lab-audio-refresh", "lab-audio-mic-1", "lab-audio-camera-1")

    # record with camera

    async def test_with_the_camera_it_runs_for_as_long_as_the_mic_records(self):
        events = self.watch()
        await self.record("lab-audio-camera-1", "20")
        self.assertEqual(events, ["camera on", "mic", "camera off"])
        self.service.audio_save.assert_awaited_once_with(1, 20)
        self.assertEqual(self.shutter.await_args_list, [mock.call(True), mock.call(False)])
        self.assertIn(SAVED["path"], self.banner_text("lab-audio-saved"))

    async def test_the_camera_stops_even_when_the_recording_fails(self):
        events = self.watch(LabError("tinycap wrote no audio"))
        await self.record("lab-audio-camera-1")
        self.assertEqual(events, ["camera on", "mic", "camera off"])
        self.assertEqual(self.banner_text("lab-error"), "tinycap wrote no audio")
        self.assertNotIn("lab-audio-saved", keys(self.view))
        self.assertFalse(self.app.lab["busy"])

    async def test_a_camera_that_wont_start_records_nothing_and_has_nothing_to_stop(self):
        self.shutter.side_effect = QuakError("the camera is busy")
        await self.record("lab-audio-camera-1")
        self.shutter.assert_awaited_once_with(True)
        self.service.audio_save.assert_not_awaited()
        self.assertEqual(self.banner_text("lab-error"), "the camera is busy")

    async def test_a_camera_that_wont_stop_is_reported_and_the_file_still_shown(self):
        self.shutter.side_effect = [None, QuakError("the camera didn't answer")]
        await self.record("lab-audio-camera-1")
        self.assertEqual(self.banner_text("lab-error"), "the camera didn't answer")
        self.assertIn(SAVED["path"], self.banner_text("lab-audio-saved"))

    async def test_taking_the_adapter_stops_the_camera_and_then_starts_over(self):
        self.service.audio_save.side_effect = [AdapterInUse("wlan0"), SAVED]
        await self.record("lab-audio-camera-1")
        self.assertEqual(self.shutter.await_args_list, [mock.call(True), mock.call(False)])
        self.assertEqual(self.app.lab["error"], "")
        await self.answer("Use it")
        self.assertEqual(self.shutter.await_args_list, [mock.call(True), mock.call(False)] * 2)
        self.assertIn(SAVED["path"], self.banner_text("lab-audio-saved"))

    # refusals

    async def test_with_no_camera_connected_it_refuses_but_the_mic_alone_still_works(self):
        with mock.patch.dict(os.environ, {"QUAK_CAMERA_ADDRESS": ADDRESS}):  # USB and host routes don't need Bluetooth
            await self.svc.disconnect()
            self.assert_enabled("lab-audio-camera-1", "lab-audio-mic-1")
            await self.press("lab-audio-camera-1")
            self.assertIsInstance(self.page.dialogs[-1], ft.SnackBar)  # no dialog asking for how long
            self.assertEqual(self.page.dialogs[-1].content.value, "Not connected. Connect from the Cameras tab.")
            self.assert_nothing_recorded()
            await self.record("lab-audio-mic-1")
            self.service.audio_save.assert_awaited_once_with(1, 30)
            self.shutter.assert_not_awaited()

    async def test_a_camera_lost_while_the_dialog_was_open_is_refused_too(self):
        await self.press("lab-audio-camera-1")
        with mock.patch.dict(os.environ, {"QUAK_CAMERA_ADDRESS": ADDRESS}):
            await self.svc.disconnect()
            await self.answer("Record")
            self.assert_nothing_recorded()
            self.assertEqual(self.banner_text("lab-error"), "Not connected. Connect from the Cameras tab.")

    async def test_a_recording_already_running_is_left_alone(self):
        self.svc.state = replace(self.svc.state, statuses={**self.svc.state.statuses, Status.ENCODING: 1})
        await self.press("lab-audio-camera-1")
        self.assertIsInstance(self.page.dialogs[-1], ft.SnackBar)
        self.assertEqual(self.page.dialogs[-1].content.value, "The camera is already recording. Stop it first.")
        self.assert_nothing_recorded()


class Service(Base):
    async def test_it_is_made_once_on_first_use_with_the_app_manager(self):
        self.make_service.assert_not_called()
        self.assertIs(self.app.lab_service, self.service)
        self.assertIs(self.app.lab_service, self.service)
        self.make_service.assert_called_once_with(self.manager, ADDRESS)
        self.make_manager.assert_called_once()
        self.assertIs(self.app.ssh_manager, self.manager)

    async def test_the_manager_has_the_app_network_context(self):
        self.app.lab_service  # noqa: B018 - make it
        self.assertEqual(self.make_manager.call_args.kwargs["network_context"], self.app._ssh_network)

    async def test_each_camera_gets_its_own_on_the_same_manager(self):
        first = self.app.lab_service
        other = mock.create_autospec(LabService, instance=True)
        self.make_service.return_value = other
        with mock.patch("quak.gui.screens.ssh._address", return_value="OTHER"):
            self.assertIs(self.app.lab_service, other)
        self.assertIs(self.app.lab_service, first)
        self.assertEqual(
            self.make_service.call_args_list,
            [mock.call(self.manager, ADDRESS), mock.call(self.manager, "OTHER")],
        )
        self.make_manager.assert_called_once()

    async def test_with_no_camera_it_says_so(self):
        await self.svc.disconnect()
        with self.assertRaisesRegex(LabError, "Not connected"):
            self.app.lab_service  # noqa: B018 - it says why

    async def test_a_tap_that_outlived_the_camera_is_reported_not_swallowed(self):
        stale = self.view
        await self.svc.disconnect()
        find(stale, "lab-refresh").on_click(None)
        await self.settle()
        self.service.status.assert_not_awaited()
        self.assertEqual(self.page.dialogs[-1].content.value, "Not connected.")
        self.assertFalse(self.app.lab["busy"])


class PasswordLogin(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.manager.profile.return_value = SshProfile(address=ADDRESS, auth="password")
        self.logged_in = mock.create_autospec(LabService, instance=True)
        self.logged_in.status.return_value = READY
        self.logged_in.storage_list.return_value = VOLUMES
        # The app's own service is the one it keeps, so an action run on that one would show up as the wrong service.
        self.make_service.side_effect = lambda manager, address, **login: self.logged_in if login else self.service

    async def question(self):
        """The password question, once it is up. ``press`` would wait for an answer that isn't coming."""
        for _ in range(500):
            await asyncio.sleep(0.001)
            if self.page.dialogs:
                return self.page.dialogs[-1]
        self.fail("it never asked")

    async def ask(self, key="lab-refresh"):
        find(self.view, key).on_click(None)
        return await self.question()

    async def log_in(self, dialog):
        dialog.content.value = SECRET
        await self.answer("Connect")

    async def test_it_asks_and_the_action_runs_on_a_service_with_what_was_typed(self):
        dialog = await self.ask()
        self.assertEqual(dialog.title.value, "SSH password")
        self.assertTrue(dialog.content.password)
        self.manager.profile.assert_called_with(ADDRESS)
        self.logged_in.status.assert_not_awaited()  # not before it has been typed
        await self.log_in(dialog)
        self.make_service.assert_called_with(self.manager, ADDRESS, password=SECRET)
        self.logged_in.status.assert_awaited_once_with()
        self.service.status.assert_not_awaited()
        self.assertEqual(self.page.dialogs, [])
        self.assertEqual(dialog.content.value, "")
        self.assertIn("Installed", words(self.view))
        self.assertFalse(self.app.lab["busy"])

    async def test_cancelling_reaches_nothing_and_the_buttons_come_back(self):
        await self.ask()
        await self.answer("Cancel")
        self.assertEqual(self.logged_in.method_calls, [])
        self.assertEqual(self.service.method_calls, [])
        self.assertNotIn(mock.call(self.manager, ADDRESS, password=mock.ANY), self.make_service.call_args_list)
        self.assertEqual(self.page.dialogs, [])
        self.assertEqual(self.app.lab["error"], "")
        self.assertFalse(self.app.lab["busy"])
        self.assert_enabled("lab-refresh")

    async def test_dismissing_the_question_is_cancelling(self):
        dialog = await self.ask()
        dialog.on_dismiss(None)
        await self.settle()
        self.assertEqual(self.logged_in.method_calls, [])
        self.assertFalse(self.app.lab["busy"])

    async def test_an_action_of_two_trips_asks_once(self):
        self.app.lab.update(address=ADDRESS, volumes=VOLUMES)
        self.open("storage")
        await self.log_in(await self.ask("lab-mount-/dev/sda1"))
        self.logged_in.storage_mount.assert_awaited_once_with("/dev/sda1", read_only=True)
        self.logged_in.storage_list.assert_awaited_once_with()
        logins = [c for c in self.make_service.call_args_list if c.kwargs]
        self.assertEqual(logins, [mock.call(self.manager, ADDRESS, password=SECRET)])
        self.assertEqual(self.page.dialogs, [])

    async def test_a_tap_from_before_cant_ask_a_second_time(self):
        stale = self.view
        find(stale, "lab-refresh").on_click(None)
        await self.question()
        find(stale, "lab-refresh").on_click(None)
        await asyncio.sleep(0.01)
        self.assertEqual(len(self.page.dialogs), 1)
        await self.answer("Cancel")

    async def test_the_next_action_asks_again_because_nothing_kept_it(self):
        await self.log_in(await self.ask())
        await self.log_in(await self.ask())
        self.assertEqual(self.logged_in.status.await_count, 2)
        for kept in (repr(self.app.lab), repr(self.app._lab_services), words(self.view)):
            self.assertNotIn(SECRET, kept)
        self.assertIs(self.app.lab_service, self.service)

    async def test_taking_the_adapter_asks_again_on_the_retry_because_nothing_kept_it(self):
        self.logged_in.status.side_effect = [AdapterInUse("wlan0"), READY]
        await self.log_in(await self.ask())
        self.assertIn("wlan0", self.page.dialogs[-1].title.value)
        next(b for b in self.page.dialogs[-1].actions if b.content == "Use it").on_click(None)
        dialog = await self.question()
        self.assertEqual(dialog.title.value, "SSH password")
        await self.log_in(dialog)
        self.assertEqual(self.logged_in.status.await_count, 2)
        self.assertIn("Installed", words(self.view))

    async def test_a_login_that_fails_is_reported_and_can_be_tried_again(self):
        self.logged_in.status.side_effect = [NetworkError("Permission denied"), READY]
        await self.log_in(await self.ask())
        self.assertEqual(self.banner_text("lab-error"), "Permission denied")
        self.assertFalse(self.app.lab["busy"])
        self.page.dialogs.clear()  # App.run's toast
        await self.log_in(await self.ask())
        self.assertNotIn("lab-error", keys(self.view))

    async def test_a_saved_profile_that_isnt_valid_is_reported_without_asking(self):
        self.manager.profile.side_effect = SshError("the saved SSH profile of SIM is not valid")
        await asyncio.wait_for(self.press("lab-refresh"), 5)  # a question here would never be answered
        self.assertEqual(self.banner_text("lab-error"), "the saved SSH profile of SIM is not valid")
        self.assertIsInstance(self.page.dialogs[-1], ft.SnackBar)
        self.assertFalse(self.app.lab["busy"])

    async def test_a_key_profile_runs_on_the_apps_service_with_no_question(self):
        self.manager.profile.return_value = SshProfile(address=ADDRESS, auth="key")
        await asyncio.wait_for(self.press("lab-refresh"), 5)  # a question here would never be answered
        self.make_service.assert_called_once_with(self.manager, ADDRESS)
        self.service.status.assert_awaited_once_with()
        self.assertEqual(self.logged_in.method_calls, [])
        self.assertEqual(self.page.dialogs, [])


class SharedWifi(Base):
    async def test_an_action_keeps_idle_wifi_until_it_is_done(self):
        self.svc.state = replace(self.svc.state, http=True)
        with (
            mock.patch.object(gui_app, "WIFI_IDLE_SECONDS", 0),
            mock.patch.object(self.svc, "release_http", mock.AsyncMock()) as release,
        ):
            self.app.lab["busy"] = True
            self.app.release_wifi_soon()
            await self.settle()
            release.assert_not_awaited()
            self.app.lab["busy"] = False
            self.app.release_wifi_soon()
            await self.settle()
            release.assert_awaited_once_with()
        self.svc.state = replace(self.svc.state, http=False)

    async def test_finishing_an_action_starts_the_wait_to_release_wifi(self):
        self.svc.state = replace(self.svc.state, http=True)
        with (
            mock.patch.object(gui_app, "WIFI_IDLE_SECONDS", 0),
            mock.patch.object(self.svc, "release_http", mock.AsyncMock()) as release,
        ):
            await self.press("lab-refresh")
            release.assert_awaited_once_with()
        self.svc.state = replace(self.svc.state, http=False)

    async def test_the_lab_waits_for_a_running_ssh_action(self):
        self.app.ssh["busy"] = True
        self.assert_disabled("lab-refresh")
        stale = self.view
        find(stale, "lab-refresh").on_click(None)
        await self.settle()
        self.service.status.assert_not_awaited()

    async def test_ssh_waits_for_a_running_lab_action(self):
        self.app.lab["busy"] = True
        view = screens.ssh(self.app)
        for key in ("ssh-arm", "ssh-test", "ssh-terminal", "ssh-refresh"):
            self.assertTrue(find(view, key).disabled, key)
        find(view, "ssh-test").on_click(None)
        await self.settle()
        self.manager.test.assert_not_awaited()


class Wiring(Base):
    async def test_more_has_the_tabs_in_order_and_the_lab_one_opens_the_screen(self):
        self.app.more_tab = "camera"
        more = screens.more(self.app)
        self.assertEqual(
            [c.key for c in find(more, "more-tab").content.controls],
            [f"more-tab-{tab}" for tab in ("camera", "lab", "ssh", "diagnostics")],
        )
        self.assertNotIn("lab-refresh", keys(more))
        find(more, "more-tab-lab").on_click(None)
        self.assertEqual(self.app.more_tab, "lab")
        find(self.page.controls, "lab-refresh")

    async def test_the_desktop_rail_has_an_item_for_it(self):
        self.page.width = 1400
        self.app.more_tab = "camera"
        self.app.render()
        item = next(c for c in walk(self.page.controls) if getattr(c, "tooltip", None) == "Lab")
        self.assertIsNone(item.bgcolor)
        item.on_click(None)
        self.assertEqual(self.app.more_tab, "lab")
        find(self.page.controls, "lab-refresh")
        item = next(c for c in walk(self.page.controls) if getattr(c, "tooltip", None) == "Lab")
        self.assertEqual(item.bgcolor, self.app.t.accent_tint)

    async def test_every_tab_builds_through_more_at_both_widths(self):
        for width in (412, 1400):
            self.page.width = width
            for tab in TABS:
                with self.subTest(width=width, tab=tab):
                    self.open(tab)
                    self.page.controls.clear()
                    self.app.render()
                    find(self.page.controls, ACTIONS[tab])

    async def test_the_wide_window_has_one_row_of_tabs_and_the_phone_two(self):
        counts = {}
        for width in (412, 1400):
            self.page.width = width
            counts[width] = keys(self.view).count("lab-tab")
        self.assertEqual(counts, {412: 2, 1400: 1})


class Toolkit(Base):
    ACTIONS = ("lab-toolkit-install", "lab-toolkit-load", "lab-toolkit-unload", "lab-toolkit-remove")
    INSTALLED = PackState(installed=True, path="/tmp/fuse_d/quak/core")

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.service.toolkit_install.return_value = self.INSTALLED
        self.service.toolkit_load.return_value = replace(self.INSTALLED, loaded=True)
        self.service.toolkit_unload.return_value = self.INSTALLED
        self.service.toolkit_remove.return_value = PackState()

    async def read_toolkit(self, pack):
        self.service.status.return_value = replace(READY, toolkit=pack)
        self.open("overview")
        await self.press("lab-refresh")

    def calls(self, name):
        return getattr(self.service, name).await_count

    def modules(self):
        """What the Kernel modules row says: walk() hands a row's controls back last first, so its value comes first."""
        found = re.search(r"([^|]+) \| Kernel modules", words(self.view))
        return found.group(1).strip() if found else None

    async def test_the_card_is_below_the_port_and_starts_unchecked(self):
        found = keys(self.view)
        # walk() hands controls back last first
        self.assertLess(found.index("lab-toolkit-install"), found.index("lab-host-off"))
        self.assertLess(found.index("lab-toolkit-remove"), found.index("lab-toolkit-unload"))
        self.assertLess(found.index("lab-toolkit-unload"), found.index("lab-toolkit-load"))
        self.assertLess(found.index("lab-toolkit-load"), found.index("lab-toolkit-install"))
        self.assertIsNone(self.modules())  # nothing to say about a pack that isn't known
        self.assert_enabled("lab-toolkit-install", "lab-toolkit-unload")
        self.assert_disabled("lab-toolkit-load", "lab-toolkit-remove")  # the card hasn't been read

    async def test_what_can_be_done_follows_what_the_card_has(self):
        await self.read_toolkit(PackState())
        self.assert_enabled("lab-toolkit-install", "lab-toolkit-unload")
        self.assert_disabled("lab-toolkit-load", "lab-toolkit-remove")
        await self.read_toolkit(self.INSTALLED)
        self.assert_enabled(*self.ACTIONS)
        self.assertEqual(self.modules(), "Not checked")
        await self.read_toolkit(PackState(error="the state file is corrupt"))
        self.assert_enabled("lab-toolkit-install", "lab-toolkit-unload")
        self.assert_disabled("lab-toolkit-load", "lab-toolkit-remove")

    async def test_install_asks_first_then_installs_and_reads_the_status_again(self):
        await self.read_toolkit(PackState())
        self.assertIn("Not installed", words(self.view))
        await self.press("lab-toolkit-install")
        self.assertIn("Nothing is loaded into the kernel", words(self.page.dialogs[-1].content))
        self.service.toolkit_install.assert_not_awaited()
        self.service.status.return_value = READY
        reads = self.calls("status")
        await self.answer("Install")
        self.service.toolkit_install.assert_awaited_once_with()
        self.assertEqual(self.calls("status"), reads + 1)
        self.assertEqual(self.banner_text("lab-toolkit-result"), "The toolkit is installed.")
        self.assertNotIn("Not installed", words(self.view))
        self.assert_enabled(*self.ACTIONS)
        self.assertEqual(self.page.dialogs, [])
        self.assertFalse(self.app.lab["busy"])

    async def test_cancelling_install_changes_nothing(self):
        await self.read_toolkit(PackState())
        await self.press("lab-toolkit-install")
        await self.answer("Cancel")
        self.service.toolkit_install.assert_not_awaited()
        self.assertEqual(self.page.dialogs, [])
        self.assertNotIn("lab-toolkit-result", keys(self.view))

    async def test_load_asks_with_the_kernel_it_needs_then_loads(self):
        await self.read_toolkit(self.INSTALLED)
        await self.press("lab-toolkit-load")
        asked = words(self.page.dialogs[-1].content)
        self.assertIn("4.19.124", asked)
        self.assertIn("4.19.124 SMP preempt mod_unload modversions aarch64", asked)
        self.assertIn("can crash the camera", asked)
        self.service.toolkit_load.assert_not_awaited()
        reads = self.calls("status")
        await self.answer("Load")
        self.service.toolkit_load.assert_awaited_once_with()
        self.assertEqual(self.calls("status"), reads + 1)
        self.assertEqual(self.banner_text("lab-toolkit-result"), "The toolkit is loaded.")
        self.assertEqual(self.modules(), "Loaded")  # the card can't say, so what the load did stays
        self.assertTrue(self.app.lab["toolkit_loaded"])
        await self.press("lab-refresh")
        self.assertEqual(self.modules(), "Loaded")

    async def test_cancelling_load_changes_nothing(self):
        await self.read_toolkit(self.INSTALLED)
        await self.press("lab-toolkit-load")
        await self.answer("Cancel")
        self.service.toolkit_load.assert_not_awaited()
        self.assertNotIn("lab-toolkit-result", keys(self.view))

    async def test_unload_asks_first_then_unloads(self):
        await self.read_toolkit(self.INSTALLED)
        await self.press("lab-toolkit-load")
        await self.answer("Load")
        await self.press("lab-toolkit-unload")
        asked = words(self.page.dialogs[-1].content)
        self.assertIn("running kernel", asked)
        self.assertIn("4.19.124", asked)
        await self.answer("Cancel")
        self.service.toolkit_unload.assert_not_awaited()
        self.assertEqual(self.modules(), "Loaded")
        await self.press("lab-toolkit-unload")
        await self.answer("Unload")
        self.service.toolkit_unload.assert_awaited_once_with()
        self.assertEqual(self.banner_text("lab-toolkit-result"), "The toolkit is unloaded.")
        self.assertEqual(self.modules(), "Not loaded")

    async def test_remove_asks_first_then_deletes_and_reads_the_status_again(self):
        await self.read_toolkit(self.INSTALLED)
        await self.press("lab-toolkit-remove")
        self.assertIn("deletes the toolkit", words(self.page.dialogs[-1].content))
        await self.answer("Cancel")
        self.service.toolkit_remove.assert_not_awaited()
        await self.press("lab-toolkit-remove")
        self.service.status.return_value = replace(READY, toolkit=PackState())
        reads = self.calls("status")
        await self.answer("Remove")
        self.service.toolkit_remove.assert_awaited_once_with()
        self.assertEqual(self.calls("status"), reads + 1)
        self.assertEqual(self.banner_text("lab-toolkit-result"), "The toolkit is removed.")
        self.assertIn("Not installed", words(self.view))
        self.assert_disabled("lab-toolkit-load", "lab-toolkit-remove")
        self.assert_enabled("lab-toolkit-install")

    async def test_nothing_can_be_done_while_an_action_runs(self):
        await self.read_toolkit(self.INSTALLED)
        self.assert_enabled(*self.ACTIONS)
        self.app.lab["busy"] = True
        self.assert_disabled(*self.ACTIONS)
        self.assertIn("Running", words(self.view))
        self.app.lab["busy"] = False
        self.app.ssh["busy"] = True  # SSH shares the camera's Wi-Fi
        self.assert_disabled(*self.ACTIONS)

    async def test_nothing_can_be_done_with_no_camera(self):
        await self.read_toolkit(self.INSTALLED)
        await self.svc.disconnect()
        self.assertIn("Not connected.", words(self.view))
        self.assert_disabled(*self.ACTIONS)

    async def test_a_failed_action_is_shown_in_the_card_and_leaves_the_status_alone(self):
        await self.read_toolkit(self.INSTALLED)
        self.service.toolkit_load.side_effect = ToolkitError("could not load f2fs: Invalid argument")
        reads = self.calls("status")
        await self.press("lab-toolkit-load")
        await self.answer("Load")
        self.assertEqual(self.banner_text("lab-toolkit-failed"), "could not load f2fs: Invalid argument")
        self.assertEqual(words(self.view).count("could not load f2fs"), 1)  # the card says it, so the top doesn't
        self.assertNotIn("lab-error", keys(self.view))
        self.open("usb")
        self.assertEqual(self.banner_text("lab-error"), "could not load f2fs: Invalid argument")  # other tabs do
        self.open("overview")
        self.assertNotIn("lab-toolkit-result", keys(self.view))
        self.assertEqual(self.calls("status"), reads)
        self.assertEqual(self.modules(), "Not checked")
        self.assert_enabled(*self.ACTIONS)  # it isn't running any more, and can be tried again
        self.service.toolkit_load.side_effect = None
        await self.press("lab-toolkit-load")
        await self.answer("Load")
        self.assertNotIn("lab-toolkit-failed", keys(self.view))  # the new attempt's, not the last one's
        self.assertEqual(self.banner_text("lab-toolkit-result"), "The toolkit is loaded.")

    async def test_a_failed_action_without_words_still_says_what_failed(self):
        await self.read_toolkit(self.INSTALLED)
        self.service.toolkit_remove.side_effect = ToolkitError()
        await self.press("lab-toolkit-remove")
        await self.answer("Remove")
        self.assertEqual(self.banner_text("lab-toolkit-failed"), "ToolkitError")

    async def test_the_cards_own_problem_is_shown_with_its_reason(self):
        await self.read_toolkit(PackState(error="the state file is corrupt"))
        self.assertIn("Problem", words(self.view))
        self.assertEqual(self.banner_text("lab-toolkit-error"), "the state file is corrupt")
        self.assertEqual(keys(self.view).count("lab-toolkit-error"), 1)

    async def test_what_an_action_did_stays_when_reading_the_status_afterwards_fails(self):
        await self.read_toolkit(self.INSTALLED)
        self.service.status.side_effect = LabError("could not read the camera's status")
        await self.press("lab-toolkit-unload")
        await self.answer("Unload")
        self.assertEqual(self.banner_text("lab-toolkit-result"), "The toolkit is unloaded.")
        self.assertEqual(self.banner_text("lab-error"), "could not read the camera's status")

    async def test_a_port_switch_makes_the_kernel_unknown_again(self):
        await self.read_toolkit(self.INSTALLED)
        await self.press("lab-toolkit-load")
        await self.answer("Load")
        self.assertEqual(self.modules(), "Loaded")
        self.service.host_off.return_value = HostModeResult(PORT_OFF, [], "")
        await self.press("lab-host-off")
        await self.answer("Exit host")
        self.assertEqual(self.modules(), "Not checked")

    async def test_another_camera_gets_none_of_the_last_ones_results(self):
        await self.read_toolkit(self.INSTALLED)
        await self.press("lab-toolkit-load")
        await self.answer("Load")
        self.app.lab["address"] = "OTHER"
        self.assertNotIn("lab-toolkit-result", keys(self.view))
        self.assertIsNone(self.modules())


@unittest.skipIf(ft is None, "GUI extra not installed")
class Translations(unittest.TestCase):
    def messages(self) -> set[str]:
        found = {"Lab"}  # the tab and the rail item, which aren't in this file
        for node in ast.walk(ast.parse(SCREEN.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and sync._called(node) in ("tr", "N_") and node.args:
                found.add(node.args[0].value)
        return found

    def test_the_screen_has_no_text_outside_the_catalogs(self):
        self.assertEqual(untranslated_text([SCREEN]), [])

    def test_the_catalogs_still_match_the_code(self):
        wanted = set(sync.wanted())
        for code in sync.languages():
            with self.subTest(code):
                have = json.loads(sync.catalog_path(code).read_text(encoding="utf-8"))
                self.assertEqual(sorted(set(have) ^ wanted), [])

    def test_every_language_has_the_screen_with_its_placeholders(self):
        for code in sync.languages():
            have = json.loads(sync.catalog_path(code).read_text(encoding="utf-8"))
            for message in sorted(self.messages()):
                with self.subTest(code=code, message=message):
                    self.assertTrue(have.get(message), "missing")
                    self.assertEqual(set(PLACEHOLDER.findall(have[message])), set(PLACEHOLDER.findall(message)))
                    self.assertEqual("Quak" in have[message], "Quak" in message)


if __name__ == "__main__":
    unittest.main()
