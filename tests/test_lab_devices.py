"""USB enumeration and classification: no hardware, no ssh, no real sysfs.

Most of this feeds the parser canned output. That says nothing about the script that produces it, so
``SnippetRuns`` also runs the real script in a local shell, over a fake sysfs tree in a temporary
directory. Whether the camera's own BusyBox agrees is still for a real camera to show.
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from quak.core.errors import LabError, NetworkError
from quak.lab.devices import LIST_SCRIPT, classify, list_devices
from quak.lab.models import UsbDevice, UsbInterface
from quak.ssh.models import ExecResult

SYSFS = "/sys/bus/usb/devices"

# In the order a shell's glob prints it, which puts each root hub's interface ahead of the hub.
BUS = """\
entry=1-0:1.0
bInterfaceClass=09
driver=hub
entry=1-1
idVendor=05e3
idProduct=0626
product=USB3.1 Hub
manufacturer=GenesysLogic
bDeviceClass=09
speed=5000
driver=usb
entry=1-1.1
idVendor=0bda
idProduct=8153
product=USB 10/100/1000 LAN
manufacturer=Realtek
bDeviceClass=00
speed=5000
driver=usb
entry=1-1.1:1.0
bInterfaceClass=ff
driver=r8152
entry=1-1.2
idVendor=0781
idProduct=5583
product=Ultra Fit
manufacturer=SanDisk
bDeviceClass=00
speed=5000
driver=usb
entry=1-1.2:1.0
bInterfaceClass=08
driver=usb-storage
entry=1-1.3
idVendor=0d8c
idProduct=0014
product=USB Audio Device
manufacturer=C-Media
bDeviceClass=00
speed=12
driver=usb
entry=1-1.3:1.0
bInterfaceClass=01
driver=snd-usb-audio
entry=1-1.3:1.1
bInterfaceClass=01
driver=snd-usb-audio
entry=1-1.3:1.3
bInterfaceClass=03
driver=usbhid
entry=1-1.4
idVendor=046d
idProduct=c31c
product=USB Keyboard
manufacturer=Logitech
bDeviceClass=00
speed=1.5
driver=usb
entry=1-1.4:1.0
bInterfaceClass=03
driver=usbhid
entry=1-1.5
idVendor=046d
idProduct=0825
product=Webcam C270
bDeviceClass=ef
speed=480
driver=usb
entry=1-1.5:1.0
bInterfaceClass=0e
driver=uvcvideo
entry=1-1.5:1.1
bInterfaceClass=0e
driver=uvcvideo
entry=1-1.5:1.2
bInterfaceClass=01
entry=1-1.6
idVendor=0403
idProduct=6001
product=FT232R USB UART
manufacturer=FTDI
bDeviceClass=00
speed=12
driver=usb
entry=1-1.6:1.0
bInterfaceClass=ff
driver=ftdi_sio
entry=1-1.7
idVendor=1234
idProduct=5678
bDeviceClass=ff
speed=480
driver=usb
entry=1-1.7:1.0
bInterfaceClass=ff
entry=1-1:1.0
bInterfaceClass=09
driver=hub
entry=2-0:1.0
bInterfaceClass=09
driver=hub
entry=usb1
idVendor=1d6b
idProduct=0002
product=xHCI Host Controller
manufacturer=Linux 4.19.124 xhci-hcd
bDeviceClass=09
speed=480
driver=usb
entry=usb2
idVendor=1d6b
idProduct=0003
product=xHCI Host Controller
manufacturer=Linux 4.19.124 xhci-hcd
bDeviceClass=09
speed=5000
driver=usb
"""

BUS_KINDS = {
    "1-1": "hub",
    "1-1.1": "network",
    "1-1.2": "storage",
    "1-1.3": "audio",
    "1-1.4": "hid",
    "1-1.5": "video",
    "1-1.6": "serial",
    "1-1.7": "vendor",
    "usb1": "hub",
    "usb2": "hub",
}


def answer(text: str | bytes = "", exit_code: int = 0, stderr: bytes = b"") -> ExecResult:
    stdout = text.encode() if isinstance(text, str) else text
    return ExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr)


class FakeRemote:
    """A ``Remote`` with one canned answer. An exception as the answer is raised."""

    def __init__(self, reply: ExecResult | Exception) -> None:
        self.reply = reply
        self.commands: list[str] = []

    def exec(self, command: str, input: bytes | None = None, timeout: float = 120.0) -> ExecResult:
        self.commands.append(command)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def read_file(self, path: str) -> bytes:
        raise AssertionError(f"enumeration should be one command, but read {path}")

    def write_file(self, path: str, data: bytes) -> None:
        raise AssertionError(f"enumeration should change nothing, but wrote {path}")


def parse(text: str | bytes) -> list[UsbDevice]:
    return list_devices(FakeRemote(answer(text)))


def by_id(devices: list[UsbDevice]) -> dict[str, UsbDevice]:
    return {device.id: device for device in devices}


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.devices = by_id(parse(BUS))

    def test_finds_each_device_and_no_interface_as_a_device(self):
        self.assertEqual(sorted(self.devices), sorted(BUS_KINDS))

    def test_reads_every_field(self):
        self.assertEqual(
            self.devices["1-1.2"],
            UsbDevice(
                id="1-1.2",
                vid="0781",
                pid="5583",
                product="Ultra Fit",
                manufacturer="SanDisk",
                bclass="00",
                speed="5000",
                driver="usb",
                interfaces=[UsbInterface(id="1-1.2:1.0", iclass="08", driver="usb-storage")],
            ),
        )

    def test_a_composite_device_keeps_each_interface_and_driver(self):
        self.assertEqual(
            self.devices["1-1.3"].interfaces,
            [
                UsbInterface(id="1-1.3:1.0", iclass="01", driver="snd-usb-audio"),
                UsbInterface(id="1-1.3:1.1", iclass="01", driver="snd-usb-audio"),
                UsbInterface(id="1-1.3:1.3", iclass="03", driver="usbhid"),
            ],
        )

    def test_an_unbound_interface_has_no_driver(self):
        self.assertEqual(self.devices["1-1.5"].interfaces[2], UsbInterface(id="1-1.5:1.2", iclass="01"))

    def test_a_root_hubs_interface_is_listed_first_and_still_lands_on_it(self):
        self.assertEqual(self.devices["usb1"].interfaces, [UsbInterface(id="1-0:1.0", iclass="09", driver="hub")])
        self.assertEqual(self.devices["usb2"].interfaces, [UsbInterface(id="2-0:1.0", iclass="09", driver="hub")])

    def test_an_interface_goes_to_its_own_tier_of_hub(self):
        text = "entry=1-1\nentry=1-1.2\nentry=1-1.2.3\nentry=1-1.2.3:1.0\nbInterfaceClass=08\n"
        devices = by_id(parse(text))
        self.assertEqual(devices["1-1"].interfaces, [])
        self.assertEqual(devices["1-1.2"].interfaces, [])
        self.assertEqual(devices["1-1.2.3"].interfaces, [UsbInterface(id="1-1.2.3:1.0", iclass="08")])

    def test_classifies_the_whole_bus(self):
        self.assertEqual({device.id: classify(device) for device in self.devices.values()}, BUS_KINDS)


class MissingFieldTests(unittest.TestCase):
    def test_a_field_not_printed_stays_empty(self):
        self.assertEqual(
            parse("entry=1-1\nidVendor=1234\nspeed=480\n"),
            [UsbDevice(id="1-1", vid="1234", speed="480")],
        )

    def test_a_field_printed_empty_stays_empty(self):
        self.assertEqual(parse("entry=1-1\nproduct=\nbDeviceClass=\n"), [UsbDevice(id="1-1")])

    def test_an_entry_with_no_fields_is_still_there(self):
        self.assertEqual(parse("entry=1-1\n"), [UsbDevice(id="1-1")])

    def test_an_interface_with_no_fields_is_still_there(self):
        self.assertEqual(parse("entry=1-1\nentry=1-1:1.0\n")[0].interfaces, [UsbInterface(id="1-1:1.0")])

    def test_a_device_nothing_is_known_about_is_other(self):
        self.assertEqual(classify(UsbDevice(id="1-1")), "other")
        self.assertEqual(classify(UsbDevice(id="1-1", interfaces=[UsbInterface(id="1-1:1.0")])), "other")

    def test_an_interface_whose_device_is_gone_is_dropped(self):
        self.assertEqual(parse("entry=1-2:1.0\nbInterfaceClass=08\nentry=1-1\n"), [UsbDevice(id="1-1")])


class MalformedLineTests(unittest.TestCase):
    def test_nothing_at_all(self):
        for text in ("", "\n", "\n\n  \n"):
            with self.subTest(repr(text)):
                self.assertEqual(parse(text), [])

    def test_lines_that_mean_nothing_are_skipped(self):
        for line in ("garbage", "=", "=value", "sh: warning", "produ", "unknownField=x", " idVendor=dead", "entry"):
            with self.subTest(line):
                self.assertEqual(
                    parse(f"entry=1-1\nidVendor=1234\n{line}\nidProduct=5678\n"),
                    [UsbDevice(id="1-1", vid="1234", pid="5678")],
                )

    def test_fields_before_any_entry_go_nowhere(self):
        self.assertEqual(parse("idVendor=dead\ndriver=evil\nentry=1-1\n"), [UsbDevice(id="1-1")])

    def test_a_bad_entry_name_takes_its_fields_with_it(self):
        for name in (
            "",
            "usb",
            "usb1-port1",
            "not a device",
            "../../etc",
            "1-",
            "1-1.",
            "-1",
            "1-1:1",
            "1-1:1.",
            "1-1:x.0",
            "1-1:1.0:1.0",
            "1-1;reboot",
        ):
            with self.subTest(name):
                self.assertEqual(
                    parse(f"entry=1-1\nidVendor=1234\nentry={name}\nidVendor=dead\ndriver=evil\n"),
                    [UsbDevice(id="1-1", vid="1234")],
                )

    def test_device_fields_on_an_interface_are_ignored(self):
        device = parse("entry=1-1\nidVendor=1234\nentry=1-1:1.0\nidVendor=dead\nproduct=evil\nbInterfaceClass=08\n")[0]
        self.assertEqual(device.vid, "1234")
        self.assertEqual(device.product, "")
        self.assertEqual(device.interfaces, [UsbInterface(id="1-1:1.0", iclass="08")])

    def test_interface_fields_on_a_device_are_ignored(self):
        self.assertEqual(parse("entry=1-1\nbInterfaceClass=08\n"), [UsbDevice(id="1-1")])

    def test_output_cut_off_midway_keeps_what_arrived(self):
        self.assertEqual(
            parse("entry=1-1\nidVendor=1234\nentry=1-2\nidVen"),
            [UsbDevice(id="1-1", vid="1234"), UsbDevice(id="1-2")],
        )

    def test_a_nonsense_class_is_other(self):
        self.assertEqual(classify(parse("entry=1-1\nbDeviceClass=zz\n")[0]), "other")

    def test_bytes_that_are_not_utf8_cost_only_themselves(self):
        device = parse(b"entry=1-1\nproduct=Disk\xff\xfe\nidVendor=1234\n")[0]
        self.assertTrue(device.product.startswith("Disk"))
        self.assertEqual(device.vid, "1234")


class WhatADeviceSaysAboutItselfTests(unittest.TestCase):
    """``product`` and ``manufacturer`` come straight from the plugged-in device."""

    def test_an_equals_sign_stays_in_the_value(self):
        self.assertEqual(parse("entry=1-1\nproduct=a=b=c\n")[0].product, "a=b=c")

    def test_padding_goes(self):
        self.assertEqual(parse("entry=1-1\nproduct=  Flash Disk   \n")[0].product, "Flash Disk")

    def test_line_breaks_other_than_newline_cannot_forge_a_record(self):
        for mark in ("\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " "):
            with self.subTest(repr(mark)):
                text = f"entry=1-1\nmanufacturer=Real\nproduct=Nice{mark}manufacturer=Fake{mark}entry=9-9\n"
                devices = parse(text)
                self.assertEqual([device.id for device in devices], ["1-1"])
                self.assertEqual(devices[0].manufacturer, "Real")
                self.assertEqual(devices[0].product, "Nicemanufacturer=Fakeentry=9-9")

    def test_terminal_escapes_are_dropped(self):
        device = parse("entry=1-1\nproduct=\x1b[2JDisk\x07\nmanufacturer=\x1b]0;owned\x07Acme\n")[0]
        self.assertEqual(device.product, "[2JDisk")
        self.assertEqual(device.manufacturer, "]0;ownedAcme")

    def test_other_languages_survive(self):
        self.assertEqual(parse("entry=1-1\nproduct=Câmera 相机\n")[0].product, "Câmera 相机")


def device_with(*interfaces: tuple[str, str], bclass: str = "00") -> UsbDevice:
    """A device from (class, driver) pairs."""
    return UsbDevice(
        id="1-1",
        bclass=bclass,
        driver="usb",
        interfaces=[UsbInterface(id=f"1-1:1.{n}", iclass=c, driver=d) for n, (c, d) in enumerate(interfaces)],
    )


class ClassifyTests(unittest.TestCase):
    def check(self, cases: dict[str, tuple[UsbDevice, str]]) -> None:
        for name, (device, kind) in cases.items():
            with self.subTest(name):
                self.assertEqual(classify(device), kind)

    def test_by_interface_class_with_no_driver_bound(self):
        self.check(
            {
                "audio": (device_with(("01", "")), "audio"),
                "cdc control": (device_with(("02", "")), "network"),
                "hid": (device_with(("03", "")), "hid"),
                "storage": (device_with(("08", "")), "storage"),
                "hub": (device_with(("09", "")), "hub"),
                "cdc data": (device_with(("0a", "")), "network"),
                "0d": (device_with(("0d", "")), "network"),
                "video": (device_with(("0e", "")), "video"),
                "vendor": (device_with(("ff", "")), "vendor"),
                "printer": (device_with(("07", "")), "other"),
                "still image": (device_with(("06", "")), "other"),
                "bluetooth": (device_with(("e0", "")), "other"),
            }
        )

    def test_by_driver_whatever_the_class_says(self):
        self.check(
            {
                "vendor-class audio": (device_with(("ff", "snd-usb-audio")), "audio"),
                "vendor-class storage": (device_with(("ff", "usb-storage")), "storage"),
                "uas": (device_with(("08", "uas")), "storage"),
                "vendor-class hid": (device_with(("ff", "usbhid")), "hid"),
                "vendor-class video": (device_with(("ff", "uvcvideo")), "video"),
                "no class at all": (device_with(("", "r8152")), "network"),
            }
        )

    def test_ethernet(self):
        self.check(
            {
                "r8152 on a vendor interface": (device_with(("ff", "r8152")), "network"),
                "asix on a vendor interface": (device_with(("ff", "asix")), "network"),
                "cdc ecm": (device_with(("02", "cdc_ether"), ("0a", "cdc_ether")), "network"),
                "cdc ncm": (device_with(("02", "cdc_ncm"), ("0a", "cdc_ncm")), "network"),
                "rndis says wireless": (device_with(("e0", "rndis_host"), ("0a", "rndis_host")), "network"),
                "unbound cdc": (device_with(("02", ""), ("0a", "")), "network"),
            }
        )

    def test_serial(self):
        self.check(
            {
                "ftdi": (device_with(("ff", "ftdi_sio")), "serial"),
                "cp210x": (device_with(("ff", "cp210x")), "serial"),
                "ch341": (device_with(("ff", "ch341"), bclass="ff"), "serial"),
                "pl2303": (device_with(("ff", "pl2303")), "serial"),
                "cdc acm shares class 02 with ethernet": (
                    device_with(("02", "cdc_acm"), ("0a", "cdc_acm"), bclass="02"),
                    "serial",
                ),
            }
        )

    def test_a_driver_it_has_never_heard_of_leaves_it_to_the_class(self):
        self.check(
            {
                "libusb on a vendor interface": (device_with(("ff", "usbfs")), "vendor"),
                "storage": (device_with(("08", "something_new")), "storage"),
                "unknown class": (device_with(("dc", "something_new")), "other"),
            }
        )

    def test_a_composite_device_is_what_it_is_mainly_for(self):
        self.check(
            {
                "webcam with a microphone": (device_with(("0e", "uvcvideo"), ("01", "snd-usb-audio")), "video"),
                "webcam whose video driver isn't loaded": (device_with(("0e", ""), ("01", "snd-usb-audio")), "video"),
                "headset with buttons": (device_with(("01", "snd-usb-audio"), ("03", "usbhid")), "audio"),
                "modem": (device_with(("08", "usb-storage"), ("ff", "option"), ("ff", "qmi_wwan")), "network"),
                "arduino leonardo": (device_with(("02", "cdc_acm"), ("0a", "cdc_acm"), ("03", "usbhid")), "serial"),
                "mouse with a vendor interface": (device_with(("03", "usbhid"), ("ff", "")), "hid"),
                "ethernet with a vendor interface": (device_with(("ff", "r8152"), ("ff", "")), "network"),
                "disk with buttons": (device_with(("08", "uas"), ("03", "usbhid")), "storage"),
            }
        )

    def test_device_class_only_when_the_interfaces_say_nothing(self):
        self.check(
            {
                "hub": (UsbDevice(id="1-1", bclass="09"), "hub"),
                "cdc": (UsbDevice(id="1-1", bclass="02"), "network"),
                "vendor": (UsbDevice(id="1-1", bclass="ff"), "vendor"),
                "per interface": (UsbDevice(id="1-1", bclass="00"), "other"),
                "composite": (UsbDevice(id="1-1", bclass="ef"), "other"),
                "interfaces unknown": (device_with(("07", ""), bclass="ff"), "vendor"),
                "interfaces outrank it": (device_with(("08", ""), bclass="ff"), "storage"),
            }
        )

    def test_the_generic_usb_driver_on_the_device_tells_nothing(self):
        self.assertEqual(classify(UsbDevice(id="1-1", driver="usb")), "other")


class RemoteTests(unittest.TestCase):
    def test_is_one_command_and_no_file_calls(self):
        remote = FakeRemote(answer(BUS))
        list_devices(remote)
        self.assertEqual(remote.commands, [LIST_SCRIPT])

    def test_a_failed_read_says_why(self):
        with self.assertRaisesRegex(LabError, "sh: broken"):
            list_devices(FakeRemote(answer(exit_code=1, stderr=b"sh: broken\n")))
        with self.assertRaisesRegex(LabError, "exit code 7"):
            list_devices(FakeRemote(answer(exit_code=7)))

    def test_a_lost_link_raises(self):
        with self.assertRaises(NetworkError):
            list_devices(FakeRemote(NetworkError("gone")))


class ScriptTextTests(unittest.TestCase):
    def test_reads_the_right_places(self):
        for piece in (
            f"for d in {SYSFS}/*;",
            "idVendor",
            "idProduct",
            "product",
            "manufacturer",
            "bDeviceClass",
            "bInterfaceClass",
            "speed",
            '"$d/driver"',
        ):
            with self.subTest(piece):
                self.assertIn(piece, LIST_SCRIPT)

    def test_changes_nothing(self):
        self.assertNotIn(">", LIST_SCRIPT.replace("2>/dev/null", ""))
        self.assertNotRegex(LIST_SCRIPT, r"\b(kill|rm|rmmod|insmod|mount|umount|mkdir|mv|cp|sleep|eval)\b")

    def test_stays_out_of_bash(self):
        self.assertNotRegex(LIST_SCRIPT, r"\[\[|<<<|<\(|=~|\$'|\bfunction\b|\blocal\b|\bdeclare\b")


def build_tree(root: Path, entries: dict[str, dict[str, str]]) -> Path:
    """A fake ``/sys/bus/usb/devices``: symlinks to the entries, whose ``driver`` is a symlink, as in sysfs."""
    tree = root / "bus" / "usb" / "devices"
    tree.mkdir(parents=True)
    for name, fields in entries.items():
        entry = root / "devices" / name
        entry.mkdir(parents=True)
        (tree / name).symlink_to(entry)
        for field, value in fields.items():
            if field == "driver":
                target = root / "bus" / "usb" / "drivers" / value
                target.mkdir(parents=True, exist_ok=True)
                (entry / "driver").symlink_to(target)
            else:
                (entry / field).write_text(f"{value}\n", encoding="utf-8")
    return tree


class LocalShell:
    """A ``Remote`` that is a shell on this computer, with the fake tree where sysfs would be."""

    def __init__(self, shell: list[str], tree: Path) -> None:
        self.shell = shell
        self.tree = tree

    def exec(self, command: str, input: bytes | None = None, timeout: float = 120.0) -> ExecResult:
        if SYSFS not in command:
            raise AssertionError("the script doesn't read sysfs")
        done = subprocess.run(
            [*self.shell, "-c", command.replace(SYSFS, f'"{self.tree}"')],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return ExecResult(exit_code=done.returncode, stdout=done.stdout, stderr=done.stderr)

    def read_file(self, path: str) -> bytes:
        raise AssertionError(f"enumeration should be one command, but read {path}")

    def write_file(self, path: str, data: bytes) -> None:
        raise AssertionError(f"enumeration should change nothing, but wrote {path}")


class SnippetRuns:
    """The real script in a real shell. Subclasses say which shell."""

    shell: list[str]

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def remote(self, entries: dict[str, dict[str, str]]) -> LocalShell:
        return LocalShell(self.shell, build_tree(self.root, entries))

    def test_reads_devices_interfaces_and_drivers(self):
        remote = self.remote(
            {
                "1-0:1.0": {"bInterfaceClass": "09", "driver": "hub"},
                "1-1": {
                    "idVendor": "0bda",
                    "idProduct": "8153",
                    "product": "USB 10/100/1000 LAN",
                    "manufacturer": "Realtek",
                    "bDeviceClass": "00",
                    "speed": "5000",
                    "driver": "usb",
                },
                "1-1:1.0": {"bInterfaceClass": "ff", "driver": "r8152"},
                "usb1": {"idVendor": "1d6b", "bDeviceClass": "09", "speed": "480", "driver": "usb"},
            }
        )
        devices = by_id(list_devices(remote))
        self.assertEqual(
            devices,
            {
                "1-1": UsbDevice(
                    id="1-1",
                    vid="0bda",
                    pid="8153",
                    product="USB 10/100/1000 LAN",
                    manufacturer="Realtek",
                    bclass="00",
                    speed="5000",
                    driver="usb",
                    interfaces=[UsbInterface(id="1-1:1.0", iclass="ff", driver="r8152")],
                ),
                "usb1": UsbDevice(
                    id="usb1",
                    vid="1d6b",
                    bclass="09",
                    speed="480",
                    driver="usb",
                    interfaces=[UsbInterface(id="1-0:1.0", iclass="09", driver="hub")],
                ),
            },
        )
        self.assertEqual(classify(devices["1-1"]), "network")
        self.assertEqual(classify(devices["usb1"]), "hub")

    def test_missing_files_and_no_driver_are_quiet(self):
        remote = self.remote({"1-1": {"idVendor": "1234"}, "1-1:1.0": {"bInterfaceClass": "08"}, "1-2": {}})
        result = remote.exec(LIST_SCRIPT)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stderr, b"")
        self.assertEqual(
            by_id(list_devices(remote)),
            {
                "1-1": UsbDevice(id="1-1", vid="1234", interfaces=[UsbInterface(id="1-1:1.0", iclass="08")]),
                "1-2": UsbDevice(id="1-2"),
            },
        )

    def test_a_product_string_stays_on_its_line(self):
        remote = self.remote(
            {
                "1-1": {"product": "Nice\nentry=9-9\nidVendor=dead", "manufacturer": "Real"},
                "1-2": {"product": "No\rentry=9-9", "manufacturer": "Real"},
            }
        )
        devices = by_id(list_devices(remote))
        self.assertEqual(sorted(devices), ["1-1", "1-2"])
        self.assertEqual(devices["1-1"].product, "Nice")
        self.assertEqual(devices["1-1"].vid, "")
        self.assertEqual(devices["1-2"].product, "Noentry=9-9")

    def test_a_product_string_is_only_ever_data(self):
        texts = ("$(echo no)", "`echo no`", "a\\nb\\\\c", "100%s %d", "-n", "'q' \"q\"", "x=$HOME *")
        products = {f"1-{port}": text for port, text in enumerate(texts, start=1)}
        remote = self.remote({device: {"product": text} for device, text in products.items()})
        self.assertEqual({device.id: device.product for device in list_devices(remote)}, products)

    def test_an_empty_bus_is_an_empty_list(self):
        self.assertEqual(list_devices(self.remote({})), [])

    def test_no_usb_in_sysfs_at_all_is_an_empty_list(self):
        self.assertEqual(list_devices(LocalShell(self.shell, self.root / "nothing" / "here")), [])


@unittest.skipUnless(shutil.which("sh"), "no sh on this computer")
class PosixShellTests(SnippetRuns, unittest.TestCase):
    shell = ["sh"]


@unittest.skipUnless(shutil.which("busybox"), "no busybox on this computer")
class BusyBoxShellTests(SnippetRuns, unittest.TestCase):
    shell = ["busybox", "sh"]


if __name__ == "__main__":
    unittest.main()
