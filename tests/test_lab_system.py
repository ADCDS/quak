"""The camera's status, logs and processes: no hardware, no ssh.

Most of this feeds the parser canned output. ``RealShellRuns`` also runs the status script in a local
shell, with temporary directories for /proc and the thermal zones.
"""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from quak.core.errors import LabError, NetworkError
from quak.lab.capabilities import system
from quak.ssh.models import ExecResult

FORBIDDEN = re.compile(r"mkfs|mke2fs|format|flash|update|\bdd\b|fdisk|parted|wipefs", re.IGNORECASE)

STATUS = """\
uptime=12345.67 23456.78
loadavg=0.10 0.25 1.50 1/123 456
mem=MemTotal:1000
mem=MemFree:200
mem=MemAvailable:600
thermal=thermal_zone0:45500:cpu-thermal
thermal=thermal_zone1:-5000:battery
df=/dev/mmcblk1p1 124733440 1000 124732440 1% /tmp/fuse_d
"""


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
        raise AssertionError(f"system runs commands, but read {path}")

    def write_file(self, path: str, data: bytes) -> None:
        raise AssertionError(f"system runs commands, but wrote {path}")


def parse(text: str) -> dict:
    return system.status(FakeRemote(answer(text)))


class StatusTests(unittest.TestCase):
    def test_reads_every_section(self):
        self.assertEqual(
            parse(STATUS),
            {
                "uptime": 12345.67,
                "load": [0.10, 0.25, 1.50],
                "mem": {"total": 1000 * 1024, "free": 200 * 1024, "available": 600 * 1024},
                "temp": [
                    {"zone": "thermal_zone0", "type": "cpu-thermal", "celsius": 45.5},
                    {"zone": "thermal_zone1", "type": "battery", "celsius": -5.0},
                ],
                "df": {
                    "path": "/tmp/fuse_d",
                    "total": 124733440 * 1024,
                    "used": 1000 * 1024,
                    "free": 124732440 * 1024,
                    "percent": 1,
                },
            },
        )

    def test_nothing_known_is_not_an_error(self):
        empty = {"uptime": None, "load": [], "mem": {}, "temp": [], "df": {}}
        for text in ("", "\n", "uptime=\nloadavg=\n", "garbage\n=\nunknown=1\n"):
            with self.subTest(repr(text)):
                self.assertEqual(parse(text), empty)

    def test_a_section_that_makes_no_sense_stays_empty(self):
        self.assertIsNone(parse("uptime=soon\n")["uptime"])
        self.assertEqual(parse("loadavg=0.1 0.2\n")["load"], [])
        self.assertEqual(parse("loadavg=a b c\n")["load"], [])
        self.assertEqual(parse("mem=MemTotal:lots\nmem=Other:5\nmem=MemFree\n")["mem"], {})
        self.assertEqual(parse("df=/dev/x - - - - /tmp/fuse_d\n")["df"], {})

    def test_a_sensor_that_could_not_be_read_is_left_out(self):
        text = "thermal=thermal_zone0::cpu\nthermal=thermal_zone1:oops:gpu\nthermal=thermal_zone2:1000:ok\n"
        self.assertEqual(parse(text)["temp"], [{"zone": "thermal_zone2", "type": "ok", "celsius": 1.0}])

    def test_a_thermal_type_can_hold_a_colon_and_no_control_characters(self):
        self.assertEqual(parse("thermal=z:1000:a:b\x1b\n")["temp"][0]["type"], "a:b")

    def test_a_device_name_with_spaces_or_numbers_does_not_move_the_columns(self):
        for device in ("my disk", "123", "1 2 3"):
            with self.subTest(device):
                df = parse(f"df={device} 4000 1000 3000 25% /tmp/fuse_d\n")["df"]
                self.assertEqual((df["total"], df["used"], df["free"]), (4096000, 1024000, 3072000))
                self.assertEqual(df["percent"], 25)

    def test_a_failed_read_says_why(self):
        with self.assertRaisesRegex(LabError, "sh: broken"):
            system.status(FakeRemote(answer(exit_code=1, stderr=b"sh: broken\n")))

    def test_a_lost_link_raises(self):
        with self.assertRaises(NetworkError):
            system.status(FakeRemote(NetworkError("gone")))

    def test_is_one_command_that_reads_the_right_places(self):
        remote = FakeRemote(answer(STATUS))
        system.status(remote)
        self.assertEqual(remote.commands, [system.STATUS_SCRIPT])
        for piece in ("/proc/uptime", "/proc/loadavg", "/proc/meminfo", "/sys/class/thermal", "df -P -k /tmp/fuse_d"):
            with self.subTest(piece):
                self.assertIn(piece, system.STATUS_SCRIPT)


class LogsTests(unittest.TestCase):
    def test_the_last_200_lines_by_default(self):
        remote = FakeRemote(answer("line 1\nline 2\n"))
        self.assertEqual(system.logs(remote), "line 1\nline 2\n")
        self.assertEqual(remote.commands, ["dmesg | tail -n 200"])

    def test_tail_is_how_many(self):
        remote = FakeRemote(answer("x\n"))
        system.logs(remote, tail=5)
        self.assertEqual(remote.commands, ["dmesg | tail -n 5"])

    def test_refuses_a_tail_that_makes_no_sense(self):
        for tail in (0, -1):
            with self.subTest(tail):
                remote = FakeRemote(answer())
                with self.assertRaises(LabError):
                    system.logs(remote, tail=tail)
                self.assertEqual(remote.commands, [])

    def test_a_tail_that_is_not_a_number_never_reaches_the_shell(self):
        remote = FakeRemote(answer())
        with self.assertRaises(TypeError):
            system.logs(remote, tail="5; reboot")
        self.assertEqual(remote.commands, [])

    def test_terminal_escapes_a_device_put_in_the_log_are_dropped(self):
        text = "usb 1-1: Product: \x1b[2JDisk\x07\r\n\tindented\n"
        self.assertEqual(system.logs(FakeRemote(answer(text))), "usb 1-1: Product: [2JDisk\n\tindented\n")

    def test_an_empty_log_is_not_an_error(self):
        self.assertEqual(system.logs(FakeRemote(answer(""))), "")

    def test_a_failed_dmesg_is_not_passed_off_as_an_empty_log(self):
        with self.assertRaisesRegex(LabError, "Operation not permitted"):
            system.logs(FakeRemote(answer("", stderr=b"dmesg: Operation not permitted\n")))

    def test_a_warning_alongside_a_log_is_not_a_failure(self):
        self.assertEqual(system.logs(FakeRemote(answer("line\n", stderr=b"a warning\n"))), "line\n")

    def test_a_non_zero_exit_says_why(self):
        with self.assertRaisesRegex(LabError, "kernel log.*exit code 3"):
            system.logs(FakeRemote(answer(exit_code=3)))

    def test_a_lost_link_raises(self):
        with self.assertRaises(NetworkError):
            system.logs(FakeRemote(NetworkError("gone")))


class ProcessesTests(unittest.TestCase):
    def test_is_what_ps_prints(self):
        text = "PID USER VSZ STAT COMMAND\n  1 root 1234 S init\n"
        remote = FakeRemote(answer(text))
        self.assertEqual(system.processes(remote), text)
        self.assertEqual(remote.commands, ["ps"])

    def test_control_characters_are_dropped(self):
        self.assertEqual(system.processes(FakeRemote(answer("1 \x1b]0;x\x07name\n"))), "1 ]0;xname\n")

    def test_a_failure_says_why(self):
        with self.assertRaisesRegex(LabError, "ps: broken"):
            system.processes(FakeRemote(answer(exit_code=1, stderr=b"ps: broken\n")))


class ChangesNothingTests(unittest.TestCase):
    def sent(self) -> list[str]:
        remote = FakeRemote(answer("x\n"))
        system.status(remote)
        system.logs(remote)
        system.processes(remote)
        return remote.commands

    def test_nothing_sent_writes_or_deletes(self):
        for command in self.sent():
            self.assertNotIn(">", command.replace("2>/dev/null", "").replace("NR>1", ""))
            self.assertNotRegex(command, r"\b(kill|rm|rmdir|rmmod|insmod|mount|umount|mkdir|mv|cp|eval)\b")

    def test_nothing_sent_is_destructive(self):
        for command in self.sent():
            self.assertNotRegex(command, FORBIDDEN)


class LocalShell:
    """A ``Remote`` that is a shell on this computer, with temporary directories where the camera's paths are."""

    def __init__(self, shell: list[str], root: Path) -> None:
        self.shell = shell
        self.root = root

    def exec(self, command: str, input: bytes | None = None, timeout: float = 120.0) -> ExecResult:
        for camera, local in (("/proc", "proc"), ("/sys/class/thermal", "thermal"), ("/tmp/fuse_d", "fuse_d")):
            command = command.replace(camera, f"{self.root}/{local}")
        done = subprocess.run(
            [*self.shell, "-c", command],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            check=False,
        )
        return ExecResult(exit_code=done.returncode, stdout=done.stdout, stderr=done.stderr)

    def read_file(self, path: str) -> bytes:
        raise AssertionError(f"system runs commands, but read {path}")

    def write_file(self, path: str, data: bytes) -> None:
        raise AssertionError(f"system runs commands, but wrote {path}")


class RealShellRuns:
    """The real status script in a real shell. Subclasses say which shell."""

    shell: list[str]

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.remote = LocalShell(self.shell, self.root)

    def test_reads_a_whole_camera(self):
        proc = self.root / "proc"
        proc.mkdir()
        (proc / "uptime").write_text("100.50 200.25\n", encoding="utf-8")
        (proc / "loadavg").write_text("0.10 0.20 0.30 1/50 99\n", encoding="utf-8")
        (proc / "meminfo").write_text(
            "MemTotal:        1000 kB\nMemFree:          200 kB\nMemAvailable:     600 kB\nCached:  5 kB\n",
            encoding="utf-8",
        )
        zone = self.root / "thermal" / "thermal_zone0"
        zone.mkdir(parents=True)
        (zone / "temp").write_text("41000\n", encoding="utf-8")
        (zone / "type").write_text("cpu\n", encoding="utf-8")
        (self.root / "thermal" / "thermal_zone1").mkdir()  # a sensor with nothing to read
        (self.root / "thermal" / "cooling_device0").mkdir()
        (self.root / "fuse_d").mkdir()

        status = system.status(self.remote)

        self.assertEqual(status["uptime"], 100.50)
        self.assertEqual(status["load"], [0.10, 0.20, 0.30])
        self.assertEqual(status["mem"], {"total": 1024000, "free": 204800, "available": 614400})
        self.assertEqual(status["temp"], [{"zone": "thermal_zone0", "type": "cpu", "celsius": 41.0}])
        self.assertGreater(status["df"]["total"], 0)
        self.assertEqual(set(status["df"]), {"path", "total", "used", "free", "percent"})

    def test_a_camera_with_none_of_it_is_quiet_and_empty(self):
        result = self.remote.exec(system.STATUS_SCRIPT)
        self.assertEqual((result.exit_code, result.stderr), (0, b""))
        self.assertEqual(
            system.status(self.remote),
            {"uptime": None, "load": [], "mem": {}, "temp": [], "df": {}},
        )


@unittest.skipUnless(shutil.which("sh"), "no sh on this computer")
class PosixShellTests(RealShellRuns, unittest.TestCase):
    shell = ["sh"]


@unittest.skipUnless(shutil.which("busybox"), "no busybox on this computer")
class BusyBoxShellTests(RealShellRuns, unittest.TestCase):
    shell = ["busybox", "sh"]


if __name__ == "__main__":
    unittest.main()
