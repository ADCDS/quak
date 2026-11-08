"""Serial ports on the camera: no hardware, no ssh, and no port is ever opened.

Most of this feeds canned output to the parser. ``RealShellRuns`` also runs the script in a local shell against a
temporary tree in the shape of /dev and sysfs, as ``test_lab_network`` does. Whether the camera's own BusyBox agrees is
still for a real camera to show.
"""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from quak.core.errors import LabError, NetworkError
from quak.lab.capabilities import serial
from quak.lab.models import SerialPort
from quak.ssh.models import ExecResult

# Same check as test_lab_hostmode: substrings on purpose, and only dd is short enough to need a word boundary.
FORBIDDEN = re.compile(r"mkfs|mke2fs|format|flash|update|\bdd\b|fdisk|parted|wipefs", re.IGNORECASE)

LISTING = """\
port=ttyUSB0
driver=ftdi_sio
usb_ids=0403:6001
port=ttyACM0
driver=cdc_acm
usb_ids=2341:0043
port=ttyS0
driver=serial8250
port=ttyS1
"""
FOUND = [
    SerialPort("/dev/ttyUSB0", driver="ftdi_sio", usb_ids="0403:6001"),
    SerialPort("/dev/ttyACM0", driver="cdc_acm", usb_ids="2341:0043"),
    SerialPort("/dev/ttyS0", driver="serial8250"),
    SerialPort("/dev/ttyS1"),
]


class FakeRemote:
    def __init__(self, text: str = "", exit_code: int = 0, stderr: bytes = b"", raises: Exception | None = None):
        self.result = ExecResult(exit_code=exit_code, stdout=text.encode(), stderr=stderr)
        self.raises = raises
        self.commands: list[str] = []

    def exec(self, command: str, input: bytes | None = None, timeout: float = 120.0) -> ExecResult:
        self.commands.append(command)
        if self.raises:
            raise self.raises
        return self.result

    def read_file(self, path: str) -> bytes:
        raise AssertionError(f"serial runs one command, but read {path}")

    def write_file(self, path: str, data: bytes) -> None:
        raise AssertionError(f"serial runs one command, but wrote {path}")


class Parsing(unittest.TestCase):
    def test_each_port_with_its_driver_and_usb_ids(self):
        self.assertEqual(serial._parse(LISTING), FOUND)

    def test_a_name_that_could_change_the_command_isnt_listed(self):
        for name in ("ttyS0;reboot", "ttyUSB0 -x", "../ttyS0", "$(reboot)", "ttyX0", "tty", ""):
            with self.subTest(name):
                self.assertEqual(serial._parse(f"port={name}\ndriver=forged\n"), [])

    def test_the_names_the_globs_can_find_are_listed(self):
        for name in ("ttyUSB10", "ttyACM2", "ttyS0", "ttySAC1"):
            with self.subTest(name):
                self.assertEqual(serial._parse(f"port={name}\n"), [SerialPort(f"/dev/{name}")])

    def test_usb_ids_are_only_ever_a_vendor_and_a_product(self):
        for ids in ("0403", ":", "0403:", ":6001", "zzzz:6001", "0403:6001:1", "0403:6001 x"):
            with self.subTest(ids):
                self.assertEqual(serial._parse(f"port=ttyUSB0\nusb_ids={ids}\n"), [SerialPort("/dev/ttyUSB0")])

    def test_a_drivers_name_cant_reach_the_terminal(self):
        self.assertEqual(serial._parse("port=ttyS0\ndriver=a\x1b[2Jb\n")[0].driver, "a[2Jb")

    def test_lines_before_the_first_port_and_without_a_value_are_skipped(self):
        found = serial._parse("driver=orphan\nnonsense\nport=ttyS0\ndriver=real\n")
        self.assertEqual(found, [SerialPort("/dev/ttyS0", driver="real")])

    def test_nothing_listed_is_an_empty_list(self):
        self.assertEqual(serial._parse(""), [])


class ListPorts(unittest.TestCase):
    def test_one_command_gives_the_list(self):
        remote = FakeRemote(LISTING)
        self.assertEqual(serial.list_ports(remote), FOUND)
        self.assertEqual(remote.commands, [serial.LIST_SCRIPT])

    def test_a_camera_with_none_is_an_empty_list(self):
        self.assertEqual(serial.list_ports(FakeRemote()), [])

    def test_a_failed_command_says_so(self):
        with self.assertRaisesRegex(LabError, "could not list the camera's serial ports: no shell"):
            serial.list_ports(FakeRemote(exit_code=1, stderr=b"no shell"))

    def test_a_lost_link_stays_a_lost_link(self):
        with self.assertRaisesRegex(NetworkError, "gone"):
            serial.list_ports(FakeRemote(raises=NetworkError("gone")))

    def test_it_looks_in_the_three_places_and_changes_nothing(self):
        self.assertIn("for dev in /dev/ttyUSB* /dev/ttyACM* /dev/ttyS*;", serial.LIST_SCRIPT)
        script = serial.LIST_SCRIPT.replace("2>/dev/null", "")
        self.assertNotIn(">", script)
        self.assertNotRegex(script, FORBIDDEN)
        self.assertNotIn("microcom", script)

    def test_it_never_opens_a_port(self):
        # Opening one can reset what's on it. The existence check is the only thing that touches the path.
        self.assertEqual(serial.LIST_SCRIPT.count('"$dev"'), 1)
        self.assertIn('[ -e "$dev" ]', serial.LIST_SCRIPT)


class MicrocomCommand(unittest.TestCase):
    def test_it_names_the_port_and_the_speed(self):
        self.assertEqual(serial.microcom_command("/dev/ttyUSB0"), "microcom -s 115200 /dev/ttyUSB0")

    def test_a_listed_name_doesnt_need_quoting_and_any_other_would_get_it(self):
        self.assertEqual(serial.microcom_command("/dev/a b"), "microcom -s 115200 '/dev/a b'")


class LocalShell:
    """Runs the command in a real shell, with a temporary folder standing in for the camera's /dev and sysfs."""

    def __init__(self, shell: list[str], root: Path) -> None:
        self.shell = shell
        self.root = root

    def exec(self, command: str, input: bytes | None = None, timeout: float = 120.0) -> ExecResult:
        command = command.replace("/sys/class/tty", f"{self.root}/class/tty").replace("/dev/tty", f"{self.root}/dev/tty")
        done = subprocess.run([*self.shell, "-c", command], capture_output=True, timeout=timeout, check=False)
        return ExecResult(exit_code=done.returncode, stdout=done.stdout, stderr=done.stderr)


class RealShellRuns:
    """The real script in a real shell. Subclasses say which shell."""

    shell: list[str]

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "dev").mkdir()
        (self.root / "class" / "tty").mkdir(parents=True)
        self.remote = LocalShell(self.shell, self.root)

    def usb_device(self, port: str, vendor: str, product: str) -> Path:
        device = self.root / "devices" / "usb1" / port
        device.mkdir(parents=True)
        (device / "idVendor").write_text(f"{vendor}\n", encoding="utf-8")
        (device / "idProduct").write_text(f"{product}\n", encoding="utf-8")
        return device

    def port(self, name: str, device: Path | None = None, driver: str = "") -> None:
        (self.root / "dev" / name).touch()
        if device is None:
            return
        device.mkdir(parents=True, exist_ok=True)
        (self.root / "class" / "tty" / name).mkdir()
        (self.root / "class" / "tty" / name / "device").symlink_to(device)
        if driver:
            target = self.root / "bus" / "drivers" / driver
            target.mkdir(parents=True, exist_ok=True)
            (device / "driver").symlink_to(target)

    def test_it_reads_a_tree_in_the_shape_of_sysfs(self):
        # ttyUSB sits below the interface, on its own usb-serial device. ttyACM's device is the interface itself.
        ftdi = self.usb_device("1-1", "0403", "6001")
        self.port("ttyUSB0", ftdi / "1-1:1.0" / "ttyUSB0", "ftdi_sio")
        arduino = self.usb_device("1-2", "2341", "0043")
        self.port("ttyACM0", arduino / "1-2:1.0", "cdc_acm")
        self.port("ttyS0", self.root / "devices" / "platform" / "serial8250", "serial8250")
        self.port("ttyS1")
        self.assertEqual(serial.list_ports(self.remote), FOUND)

    def test_with_no_ports_the_globs_find_nothing_and_thats_fine(self):
        self.assertEqual(serial.list_ports(self.remote), [])

    def test_a_usb_port_with_no_driver_still_has_its_ids(self):
        self.port("ttyUSB0", self.usb_device("1-1", "0403", "6001") / "1-1:1.0" / "ttyUSB0")
        self.assertEqual(serial.list_ports(self.remote), [SerialPort("/dev/ttyUSB0", usb_ids="0403:6001")])

    def test_each_port_finds_its_own_device_and_not_a_neighbours(self):
        self.usb_device("1-1", "0403", "6001")
        self.port("ttyS0", self.root / "devices" / "usb1" / "1-1-sibling" / "port")
        self.assertEqual(serial.list_ports(self.remote), [SerialPort("/dev/ttyS0")])

    def test_other_ttys_arent_listed(self):
        for name in ("tty", "tty1", "ttyGS0"):
            (self.root / "dev" / name).touch()
        self.assertEqual(serial.list_ports(self.remote), [])


@unittest.skipUnless(shutil.which("sh"), "no sh on this computer")
class PosixShellTests(RealShellRuns, unittest.TestCase):
    shell = ["sh"]


@unittest.skipUnless(shutil.which("busybox"), "no busybox on this computer")
class BusyBoxShellTests(RealShellRuns, unittest.TestCase):
    shell = ["busybox", "sh"]


if __name__ == "__main__":
    unittest.main()
