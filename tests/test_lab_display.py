"""Framebuffers on the camera: no hardware, no ssh.

Most of this feeds canned output to the parser. ``RealShellRuns`` also runs the script in a local shell against a
temporary tree in the shape of sysfs, as ``test_lab_network`` does. Whether the camera's own BusyBox agrees is still for a
real camera to show.
"""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from quak.core.errors import LabError, NetworkError
from quak.lab.capabilities import display
from quak.lab.models import Framebuffer
from quak.ssh.models import ExecResult

# Same check as test_lab_hostmode: substrings on purpose, and only dd is short enough to need a word boundary.
FORBIDDEN = re.compile(r"mkfs|mke2fs|format|flash|update|\bdd\b|fdisk|parted|wipefs", re.IGNORECASE)

LISTING = """\
entry=fb0
name=mxc_lcd
virtual_size=1920,1080
bits_per_pixel=32
state=0
entry=fb1
name=udlfb
virtual_size=1280,720
bits_per_pixel=16
state=1
entry=fbcon
state=0
"""
FOUND = [
    Framebuffer("mxc_lcd", virtual_size="1920x1080", bits_per_pixel=32, state="running"),
    Framebuffer("udlfb", virtual_size="1280x720", bits_per_pixel=16, state="suspended"),
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
        raise AssertionError(f"display runs one command, but read {path}")

    def write_file(self, path: str, data: bytes) -> None:
        raise AssertionError(f"display runs one command, but wrote {path}")


class Parsing(unittest.TestCase):
    def test_each_framebuffer_with_its_size_depth_and_state(self):
        self.assertEqual(display._parse(LISTING), FOUND)

    def test_the_console_beside_them_isnt_one(self):
        self.assertEqual(display._parse("entry=fbcon\nname=forged\n"), [])
        self.assertEqual(display._parse("entry=fb0x\nname=forged\n"), [])

    def test_what_the_camera_cant_say_stays_empty(self):
        self.assertEqual(display._parse("entry=fb0\n"), [Framebuffer("")])
        self.assertEqual(display._parse("entry=fb0\nname=\nbits_per_pixel=\nstate=\n"), [Framebuffer("")])
        self.assertEqual(display._parse("entry=fb0\nbits_per_pixel=lots\n")[0].bits_per_pixel, 0)

    def test_a_state_with_no_word_stays_as_the_kernel_has_it(self):
        self.assertEqual(display._parse("entry=fb0\nstate=7\n")[0].state, "7")

    def test_a_drivers_name_cant_reach_the_terminal_or_forge_a_record(self):
        found = display._parse("entry=fb0\nname=a\x1b[2Jb\rentry=fb9 c\n")
        self.assertEqual(found, [Framebuffer("a[2Jbentry=fb9c")])

    def test_lines_before_the_first_entry_and_without_a_value_are_skipped(self):
        self.assertEqual(display._parse("name=orphan\nnonsense\nentry=fb0\nname=real\n"), [Framebuffer("real")])

    def test_nothing_listed_is_an_empty_list(self):
        self.assertEqual(display._parse(""), [])


class ListFramebuffers(unittest.TestCase):
    def test_one_command_gives_the_list(self):
        remote = FakeRemote(LISTING)
        self.assertEqual(display.list_framebuffers(remote), FOUND)
        self.assertEqual(remote.commands, [display.LIST_SCRIPT])

    def test_a_camera_with_none_is_an_empty_list(self):
        self.assertEqual(display.list_framebuffers(FakeRemote()), [])

    def test_a_failed_command_says_so(self):
        with self.assertRaisesRegex(LabError, "could not list the camera's framebuffers: no shell"):
            display.list_framebuffers(FakeRemote(exit_code=1, stderr=b"no shell"))

    def test_a_lost_link_stays_a_lost_link(self):
        with self.assertRaisesRegex(NetworkError, "gone"):
            display.list_framebuffers(FakeRemote(raises=NetworkError("gone")))

    def test_it_reads_the_four_files_and_changes_nothing(self):
        self.assertIn("/sys/class/graphics/fb*", display.LIST_SCRIPT)
        self.assertIn("for f in name virtual_size bits_per_pixel state;", display.LIST_SCRIPT)
        script = display.LIST_SCRIPT.replace("2>/dev/null", "")
        self.assertNotIn(">", script)
        self.assertNotRegex(script, FORBIDDEN)


class LocalShell:
    """Runs the command in a real shell, with a temporary folder standing in for the camera's sysfs."""

    def __init__(self, shell: list[str], root: Path) -> None:
        self.shell = shell
        self.root = root

    def exec(self, command: str, input: bytes | None = None, timeout: float = 120.0) -> ExecResult:
        command = command.replace(display.GRAPHICS, str(self.root))
        done = subprocess.run([*self.shell, "-c", command], capture_output=True, timeout=timeout, check=False)
        return ExecResult(exit_code=done.returncode, stdout=done.stdout, stderr=done.stderr)


class RealShellRuns:
    """The real script in a real shell. Subclasses say which shell."""

    shell: list[str]

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.remote = LocalShell(self.shell, self.root)

    def framebuffer(self, entry: str, **files: str) -> None:
        folder = self.root / entry
        folder.mkdir()
        for name, value in files.items():
            (folder / name).write_text(f"{value}\n", encoding="utf-8")

    def test_it_reads_a_sysfs_tree(self):
        self.framebuffer("fb0", name="mxc_lcd", virtual_size="1920,1080", bits_per_pixel="32", state="0")
        self.framebuffer("fb1", name="udlfb", virtual_size="1280,720", bits_per_pixel="16", state="1")
        self.framebuffer("fbcon", state="0")
        self.assertEqual(display.list_framebuffers(self.remote), FOUND)

    def test_with_no_framebuffers_the_glob_finds_nothing_and_thats_fine(self):
        self.assertEqual(display.list_framebuffers(self.remote), [])

    def test_a_file_thats_missing_leaves_its_field_empty(self):
        self.framebuffer("fb0", name="sparse")
        self.assertEqual(display.list_framebuffers(self.remote), [Framebuffer("sparse")])

    def test_only_a_names_first_line_is_read(self):
        (self.root / "fb0").mkdir()
        (self.root / "fb0" / "name").write_text("real\nentry=fb9\n", encoding="utf-8")
        self.assertEqual(display.list_framebuffers(self.remote), [Framebuffer("real")])


@unittest.skipUnless(shutil.which("sh"), "no sh on this computer")
class PosixShellTests(RealShellRuns, unittest.TestCase):
    shell = ["sh"]


@unittest.skipUnless(shutil.which("busybox"), "no busybox on this computer")
class BusyBoxShellTests(RealShellRuns, unittest.TestCase):
    shell = ["busybox", "sh"]


if __name__ == "__main__":
    unittest.main()
