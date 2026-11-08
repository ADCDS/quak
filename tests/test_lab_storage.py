"""Storage on the camera: no hardware, no ssh, no real mount.

Most of this looks at the commands sent and feeds canned output back. That says nothing about whether
the scripts work, so ``RealShellRuns`` also runs them in a local shell, over temporary directories, with
shell functions standing in for mount, umount and blkid. Functions rather than stubs on PATH, because
they win over a BusyBox applet too, so these tests can't reach the real mount. Whether the camera's own
BusyBox agrees is still for a real camera to show.
"""

import hashlib
import inspect
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from quak.core.errors import LabError, NetworkError
from quak.lab.capabilities import storage
from quak.lab.capabilities.storage import CopyResult, DirEntry
from quak.lab.models import Volume
from quak.ssh.models import ExecResult

SHA_A = "a" * 64
SHA_B = "b" * 64

# Same check as test_lab_hostmode: substrings on purpose, and only dd is short enough to need a word boundary.
FORBIDDEN = re.compile(r"mkfs|mke2fs|format|flash|update|\bdd\b|fdisk|parted|wipefs", re.IGNORECASE)

# Invented paths, in the shape sysfs has: a partition's folder inside its disk's.
LISTING = """\
entry=mmcblk1
size=124735488
path=/sys/devices/platform/mmc1/block/mmcblk1
blkid=
entry=mmcblk1p1
size=124733440
path=/sys/devices/platform/mmc1/block/mmcblk1/mmcblk1p1
blkid=/dev/mmcblk1p1: LABEL="GOPRO" UUID="1234-ABCD" TYPE="exfat"
entry=sda
size=2000000
path=/sys/devices/platform/usb1/1-1/block/sda
blkid=/dev/sda: PTUUID="abc" PTTYPE="dos"
entry=sda1
size=1000000
path=/sys/devices/platform/usb1/1-1/block/sda/sda1
blkid=/dev/sda1: LABEL="MY DISK" UUID="55" TYPE="vfat"
entry=sda2
size=1000000
path=/sys/devices/platform/usb1/1-1/block/sda/sda2
blkid=/dev/sda2: SEC_TYPE="msdos" LABEL_FATBOOT="NOPE" PARTLABEL="NOPE"
entry=sdb
size=4096
path=/sys/devices/platform/usb1/1-2/block/sdb
blkid=/dev/sdb: TYPE="ext4"
entry=sdc
size=0
path=/sys/devices/platform/usb1/1-3/block/sdc
blkid=
mount=proc /proc proc rw,relatime 0 0
mount=/dev/mmcblk1p1 /tmp/fuse_d exfat rw,relatime 0 0
mount=/dev/sda1 /tmp/quak/mnt/sda1 vfat ro,nosuid,nodev,noexec,relatime 0 0
"""

NASTY_PATHS = (
    "/tmp/fuse_d/a b.mp4",
    "/tmp/fuse_d/it's.mp4",
    "/tmp/fuse_d/$(reboot).mp4",
    "/tmp/fuse_d/`reboot`.mp4",
    "/tmp/fuse_d/x; reboot #.mp4",
    '/tmp/fuse_d/q"q.mp4',
    "/tmp/fuse_d/back\\slash.mp4",
    "/tmp/fuse_d/new\nline.mp4",
    "/tmp/fuse_d/*.mp4",
)

BAD_DEVICES = ("sda1; reboot", "$(reboot)", "`reboot`", "a b", "../sda", "/dev/../sda", "sda\n", "", "/dev/", "-o")
BAD_DEVICES += ("/etc/passwd", "/tmp/fuse_d/x", "/dev/sda/../mmcblk0")

BAD_MOUNTPOINTS = (
    "/",
    "/etc",
    "/tmp/fuse_d",
    "/tmp/fuse_d/quak",
    "/tmp/quak/mnt",
    "/tmp/quak/mnt/",
    "/tmp/quak/mnt/..",
    "/tmp/quak/mnt/../../fuse_d",
    "/tmp/quak/mnt/a/b",
    "/tmp/quak/mntx/a",
    "//tmp/quak/mnt/a",
    "../x",
    "a/b",
    "..",
    ".",
    "",
    "...",
    "---",
)

# Anywhere a copy could overwrite a disk or the system, spelled every way that resolves there.
BAD_COPY_PATHS = (
    "/",
    "/dev/sda",
    "/dev/sda1",
    "/dev/mmcblk0",
    "/dev/zero",
    "/proc/sysrq-trigger",
    "/sys/block/sda/x",
    "/etc/passwd",
    "/usr/local/gopro/bin/gpsv",
    "/tmp/x",
    "/tmp/fuse_dx/y",
    "/tmp/quak/mnt",
    "/tmp/quak/mntx/y",
    "/tmp/fuse_d/../../dev/sda",
    "/tmp/quak/mnt/../../../dev/sda",
    "//dev/sda",
    "relative/path",
    "-rf",
    "",
    "/tmp/fuse_d/a\0b",
)


def answer(text: str | bytes = "", exit_code: int = 0, stderr: bytes = b"") -> ExecResult:
    stdout = text.encode() if isinstance(text, str) else text
    return ExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr)


class FakeRemote:
    """A ``Remote`` with one canned answer. An exception as the answer is raised."""

    def __init__(self, reply: ExecResult | Exception | None = None) -> None:
        self.reply = reply if reply is not None else answer()
        self.commands: list[str] = []
        self.timeouts: list[float] = []

    def exec(self, command: str, input: bytes | None = None, timeout: float = 120.0) -> ExecResult:
        self.commands.append(command)
        self.timeouts.append(timeout)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def read_file(self, path: str) -> bytes:
        raise AssertionError(f"storage runs commands, but read {path}")

    def write_file(self, path: str, data: bytes) -> None:
        raise AssertionError(f"storage runs commands, but wrote {path}")


def assignment(command: str, name: str) -> str:
    """What a shell would assign to ``name`` in the script's first lines."""
    tokens = shlex.split(command, comments=False)
    prefix = f"{name}="
    return next(token.removeprefix(prefix) for token in tokens if token.startswith(prefix))


def copied(*, sha: str = SHA_A, other: str | None = None, into: bool = False) -> ExecResult:
    head = "into=1\n" if into else ""
    return answer(f"{head}source={sha}\ncopy={other or sha}\n")


class ListVolumesTests(unittest.TestCase):
    def setUp(self):
        self.volumes = {volume.device: volume for volume in storage.list_volumes(FakeRemote(answer(LISTING)))}

    def test_a_mounted_volume_says_where_and_how(self):
        self.assertEqual(
            self.volumes["/dev/sda1"],
            Volume(
                device="/dev/sda1",
                label="MY DISK",
                fs="vfat",
                size=1000000 * 512,
                mountpoint="/tmp/quak/mnt/sda1",
                mounted=True,
                read_only=True,
            ),
        )

    def test_a_read_write_mount_says_so(self):
        self.assertFalse(self.volumes["/dev/mmcblk1p1"].read_only)
        self.assertEqual(self.volumes["/dev/mmcblk1p1"].mountpoint, "/tmp/fuse_d")

    def test_an_unmounted_volume(self):
        self.assertEqual(self.volumes["/dev/sdb"], Volume(device="/dev/sdb", fs="ext4", size=4096 * 512))

    def test_a_disk_with_partitions_is_not_a_volume(self):
        self.assertNotIn("/dev/sda", self.volumes)
        self.assertNotIn("/dev/mmcblk1", self.volumes)

    def test_a_disk_with_a_filesystem_and_no_partitions_is_one(self):
        self.assertIn("/dev/sdb", self.volumes)

    def test_a_drive_with_nothing_in_it_is_not_a_volume(self):
        self.assertNotIn("/dev/sdc", self.volumes)

    def test_only_the_real_label_and_type_fields_count(self):
        self.assertEqual((self.volumes["/dev/sda2"].label, self.volumes["/dev/sda2"].fs), ("", ""))

    def test_a_volume_nothing_recognised_is_still_listed(self):
        volumes = storage.list_volumes(FakeRemote(answer("entry=sda1\nsize=10\nblkid=\n")))
        self.assertEqual(volumes, [Volume(device="/dev/sda1", size=5120)])

    def test_without_blkid_a_mounted_volume_takes_its_type_from_the_mount(self):
        text = "entry=sda1\nsize=10\nmount=/dev/sda1 /tmp/quak/mnt/sda1 vfat rw 0 0\n"
        self.assertEqual(storage.list_volumes(FakeRemote(answer(text)))[0].fs, "vfat")

    def test_lines_that_mean_nothing_are_skipped(self):
        text = "garbage\nsize=99\nentry=../../etc\nsize=99\nentry=a;reboot\nsize=99\nmount=short\nentry=sda1\nsize=x\n"
        self.assertEqual(storage.list_volumes(FakeRemote(answer(text))), [])

    def test_no_block_devices_is_an_empty_list(self):
        self.assertEqual(storage.list_volumes(FakeRemote(answer(""))), [])

    def test_a_failed_read_says_why(self):
        with self.assertRaisesRegex(LabError, "sh: broken"):
            storage.list_volumes(FakeRemote(answer(exit_code=1, stderr=b"sh: broken\n")))
        with self.assertRaisesRegex(LabError, "exit code 7"):
            storage.list_volumes(FakeRemote(answer(exit_code=7)))

    def test_a_lost_link_raises(self):
        with self.assertRaises(NetworkError):
            storage.list_volumes(FakeRemote(NetworkError("gone")))

    def test_is_one_command_that_reads_the_right_places_and_changes_nothing(self):
        remote = FakeRemote(answer(LISTING))
        storage.list_volumes(remote)
        self.assertEqual(remote.commands, [storage.LIST_SCRIPT])
        self.assertIn("/sys/class/block/*", storage.LIST_SCRIPT)
        self.assertIn('blkid "/dev/$name"', storage.LIST_SCRIPT)
        self.assertNotIn(">", storage.LIST_SCRIPT.replace("2>/dev/null", ""))
        self.assertNotRegex(storage.LIST_SCRIPT, r"\b(rm|mkdir|mv|cp|umount|eval)\b|\bmount\b(?!=)")


class WhatADiskSaysAboutItselfTests(unittest.TestCase):
    """A label comes straight from the plugged-in disk, and BusyBox's blkid prints it raw."""

    def label_and_fs(self, blkid: str) -> tuple[str, str]:
        volume = storage.list_volumes(FakeRemote(answer(f"entry=sda1\nsize=10\nblkid={blkid}\n")))[0]
        return volume.label, volume.fs

    def test_a_label_cannot_change_the_type(self):
        self.assertEqual(
            self.label_and_fs('/dev/sda1: LABEL="x" TYPE="ntfs" UUID="1" TYPE="vfat"'),
            ("x", "vfat"),
        )

    def test_escaped_quotes_stay_in_the_label(self):
        self.assertEqual(self.label_and_fs(r'/dev/sda1: LABEL="a\"b\\c" TYPE="vfat"'), ('a"b\\c', "vfat"))

    def test_terminal_escapes_are_dropped(self):
        self.assertEqual(self.label_and_fs('/dev/sda1: LABEL="\x1b[2JDisk\x07" TYPE="vfat"'), ("[2JDisk", "vfat"))

    def test_line_breaks_other_than_newline_cannot_forge_a_record(self):
        for mark in ("\r", "\x0b", "\x0c", "\x1c", "\x85", " "):
            with self.subTest(repr(mark)):
                text = f'entry=sda1\nsize=10\nblkid=/dev/sda1: LABEL="a{mark}entry=sdz{mark}size=99"\n'
                self.assertEqual([v.device for v in storage.list_volumes(FakeRemote(answer(text)))], ["/dev/sda1"])

    def test_other_languages_survive(self):
        self.assertEqual(self.label_and_fs('/dev/sda1: LABEL="Câmera 相机"')[0], "Câmera 相机")


class MountTests(unittest.TestCase):
    def test_read_only_by_default_at_a_name_from_the_device(self):
        remote = FakeRemote(answer("mounted=vfat ro,nosuid,nodev,noexec,relatime\n"))
        volume = storage.mount(remote, "/dev/sda1")
        self.assertEqual(len(remote.commands), 1)
        self.assertIn('mount -o ro,nosuid,nodev,noexec "$dev" "$mp"', remote.commands[0])
        self.assertNotRegex(remote.commands[0], r"\brw\b")
        self.assertEqual(assignment(remote.commands[0], "dev"), "/dev/sda1")
        self.assertEqual(assignment(remote.commands[0], "mp"), "/tmp/quak/mnt/sda1")
        self.assertEqual(
            volume,
            Volume(device="/dev/sda1", fs="vfat", mountpoint="/tmp/quak/mnt/sda1", mounted=True, read_only=True),
        )

    def test_a_bare_device_name_works_too(self):
        remote = FakeRemote(answer("mounted=vfat ro\n"))
        self.assertEqual(storage.mount(remote, "mmcblk1p2").device, "/dev/mmcblk1p2")

    def test_read_write_takes_asking_for_it(self):
        remote = FakeRemote(answer("mounted=vfat rw,nosuid,nodev,noexec\n"))
        volume = storage.mount(remote, "sda1", read_only=False)
        self.assertIn('mount -o rw,nosuid,nodev,noexec "$dev" "$mp"', remote.commands[0])
        self.assertFalse(volume.read_only)

    def test_anything_but_an_explicit_false_stays_read_only(self):
        for value in (True, None, 0, ""):
            with self.subTest(repr(value)):
                remote = FakeRemote(answer("mounted=vfat ro\n"))
                storage.mount(remote, "sda1", read_only=value)
                self.assertIn("mount -o ro,", remote.commands[0])

    def test_the_kernels_answer_wins(self):
        remote = FakeRemote(answer("mounted=vfat ro,relatime\n"))
        self.assertTrue(storage.mount(remote, "sda1", read_only=False).read_only)

    def test_never_setuid_devices_or_execution_even_read_write(self):
        for read_only in (True, False):
            remote = FakeRemote(answer("mounted=vfat ro\n"))
            storage.mount(remote, "sda1", read_only=read_only)
            self.assertRegex(remote.commands[0], r"mount -o \w+,nosuid,nodev,noexec ")

    def test_a_mountpoint_by_name_or_by_whole_path(self):
        for given in ("usb", "/tmp/quak/mnt/usb", "/tmp/quak/mnt/usb/", "/tmp/quak/mnt/x/../usb"):
            with self.subTest(given):
                remote = FakeRemote(answer("mounted=vfat ro\n"))
                self.assertEqual(storage.mount(remote, "sda1", given).mountpoint, "/tmp/quak/mnt/usb")

    def test_a_name_is_made_safe(self):
        cases = {
            "My Disk": "My_Disk",
            "a;reboot": "a_reboot",
            "$(reboot)": "__reboot_",
            "`reboot`": "_reboot_",
            ".hidden": "hidden",
            "-rf": "rf",
            "x\ny": "x_y",
            "câmera": "c_mera",
            "/tmp/quak/mnt/My Disk": "My_Disk",
        }
        for given, name in cases.items():
            with self.subTest(given):
                remote = FakeRemote(answer("mounted=vfat ro\n"))
                self.assertEqual(storage.mount(remote, "sda1", given).mountpoint, f"/tmp/quak/mnt/{name}")
                self.assertEqual(assignment(remote.commands[0], "mp"), f"/tmp/quak/mnt/{name}")

    def test_refuses_a_bad_device_before_sending_anything(self):
        for device in BAD_DEVICES:
            with self.subTest(repr(device)):
                remote = FakeRemote()
                with self.assertRaises(LabError):
                    storage.mount(remote, device)
                self.assertEqual(remote.commands, [])

    def test_refuses_a_mountpoint_elsewhere_before_sending_anything(self):
        for mountpoint in BAD_MOUNTPOINTS:
            with self.subTest(repr(mountpoint)):
                remote = FakeRemote()
                with self.assertRaises(LabError):
                    storage.mount(remote, "sda1", mountpoint)
                self.assertEqual(remote.commands, [])

    def test_a_failure_says_why(self):
        with self.assertRaisesRegex(LabError, "/dev/sda1.*bad superblock"):
            storage.mount(FakeRemote(answer(exit_code=1, stderr=b"mount: bad superblock\n")), "sda1")


class UnmountTests(unittest.TestCase):
    def test_unmounts_and_removes_only_its_own_empty_folder(self):
        remote = FakeRemote()
        self.assertIsNone(storage.unmount(remote, "/tmp/quak/mnt/sda1"))
        self.assertEqual(
            remote.commands,
            ["umount /tmp/quak/mnt/sda1 && { rmdir /tmp/quak/mnt/sda1 2>/dev/null || true; }\n"],
        )

    def test_by_name_too(self):
        remote = FakeRemote()
        storage.unmount(remote, "sda1")
        self.assertIn("umount /tmp/quak/mnt/sda1 ", remote.commands[0])

    def test_eject_is_unmount(self):
        self.assertIs(storage.eject, storage.unmount)

    def test_never_forces(self):
        remote = FakeRemote()
        storage.eject(remote, "sda1")
        self.assertNotRegex(remote.commands[0], r"umount\s+-")

    def test_refuses_the_sd_card_and_anything_else_outside_the_lab(self):
        for mountpoint in BAD_MOUNTPOINTS:
            with self.subTest(repr(mountpoint)):
                remote = FakeRemote()
                with self.assertRaises(LabError):
                    storage.unmount(remote, mountpoint)
                self.assertEqual(remote.commands, [])

    def test_a_busy_volume_says_why(self):
        with self.assertRaisesRegex(LabError, "busy"):
            storage.unmount(FakeRemote(answer(exit_code=1, stderr=b"umount: can't unmount: Device busy\n")), "sda1")


class ListDirTests(unittest.TestCase):
    def listing(self, *records: tuple[bytes, bytes, bytes]) -> list[DirEntry]:
        data = b"".join(field + b"\0" for record in records for field in record)
        return storage.list_dir(FakeRemote(answer(data)), "/tmp/fuse_d")

    def test_reads_name_type_and_size(self):
        self.assertEqual(
            self.listing((b"file", b"1234", b"a.mp4"), (b"dir", b"0", b"DCIM"), (b"link", b"0", b"l")),
            [DirEntry("a.mp4", "file", 1234), DirEntry("DCIM", "dir", 0), DirEntry("l", "link", 0)],
        )

    def test_each_is_a_name_type_size_tuple(self):
        name, kind, size = self.listing((b"file", b"5", b"a"))[0]
        self.assertEqual((name, kind, size), ("a", "file", 5))

    def test_a_size_over_4_gb_survives(self):
        self.assertEqual(self.listing((b"file", b"4294967296", b"a"))[0].size, 4294967296)

    def test_a_file_gone_mid_read_has_no_size(self):
        self.assertEqual(self.listing((b"file", b"", b"a")), [DirEntry("a", "file", 0)])

    def test_an_empty_folder_is_an_empty_list(self):
        self.assertEqual(storage.list_dir(FakeRemote(answer(b"")), "/tmp/fuse_d"), [])

    def test_a_name_cannot_forge_a_record(self):
        self.assertEqual(self.listing((b"file", b"1", b"a\nfile\t9\tevil")), [DirEntry("afile9evil", "file", 1)])

    def test_terminal_escapes_are_dropped_and_bad_bytes_cost_only_themselves(self):
        self.assertEqual(self.listing((b"file", b"1", b"\x1b[2Ja\x07.mp4"))[0].name, "[2Ja.mp4")
        self.assertTrue(self.listing((b"file", b"1", b"a\xff.mp4"))[0].name.startswith("a"))

    def test_output_cut_off_midway_keeps_what_arrived(self):
        data = b"file\x001\x00a\x00file\x002"
        self.assertEqual(storage.list_dir(FakeRemote(answer(data)), "/x"), [DirEntry("a", "file", 1)])

    def test_every_path_is_quoted(self):
        for path in NASTY_PATHS:
            with self.subTest(path):
                remote = FakeRemote(answer(b""))
                storage.list_dir(remote, path)
                self.assertEqual(remote.commands[0].partition(" || exit 1")[0], f"cd {shlex.quote(path)}")

    def test_refuses_a_path_that_is_not_absolute(self):
        for path in ("", "relative", "-la", "./x", "a\0b", "/a\0b"):
            with self.subTest(repr(path)):
                remote = FakeRemote()
                with self.assertRaises(LabError):
                    storage.list_dir(remote, path)
                self.assertEqual(remote.commands, [])

    def test_a_missing_folder_says_why(self):
        with self.assertRaisesRegex(LabError, "/nope.*can't cd"):
            storage.list_dir(FakeRemote(answer(exit_code=1, stderr=b"sh: cd: can't cd to /nope\n")), "/nope")

    def test_changes_nothing(self):
        remote = FakeRemote(answer(b""))
        storage.list_dir(remote, "/tmp/fuse_d")
        self.assertNotIn(">", remote.commands[0].replace("2>/dev/null", ""))
        self.assertNotRegex(remote.commands[0], r"\b(rm|mkdir|mv|cp|mount|umount|eval)\b")


class CopyCommandTests(unittest.TestCase):
    SRC = "/tmp/fuse_d/DCIM/a.mp4"
    DST = "/tmp/quak/mnt/sda1/a.mp4"

    def test_every_path_is_quoted(self):
        for path in NASTY_PATHS:
            with self.subTest(path):
                remote = FakeRemote(copied())
                storage.copy(remote, path, self.DST)
                self.assertIn(f"src={shlex.quote(path)}\n", remote.commands[0])
                self.assertEqual(assignment(remote.commands[0], "src"), path)
                remote = FakeRemote(copied())
                storage.copy(remote, self.SRC, path)
                self.assertIn(f"dst={shlex.quote(path)}\n", remote.commands[0])
                self.assertEqual(assignment(remote.commands[0], "dst"), path)

    def test_the_paths_are_only_used_quoted_inside_the_script(self):
        remote = FakeRemote(copied())
        storage.copy(remote, self.SRC, self.DST)
        body = remote.commands[0].split("vol=", 1)[1]
        self.assertNotRegex(body.replace('"$src"', "").replace('"$part"', ""), r'(cp|sha256sum <|rm -f) \$')
        self.assertIn('cp "$src" "$part"', body)
        self.assertIn('sha256sum < "$src"', body)
        self.assertIn('sha256sum < "$part"', body)
        self.assertIn('cp -R "$src" "$target"', body)

    def test_checks_a_destination_volume_is_mounted(self):
        remote = FakeRemote(copied())
        storage.copy(remote, self.SRC, "/tmp/quak/mnt/sda1/deep/er/a.mp4")
        self.assertEqual(assignment(remote.commands[0], "vol"), "/tmp/quak/mnt/sda1")
        remote = FakeRemote(copied())
        storage.copy(remote, self.DST, self.SRC)
        self.assertEqual(assignment(remote.commands[0], "vol"), "")

    def test_a_copy_gets_a_long_timeout_a_caller_can_change(self):
        remote = FakeRemote(copied())
        storage.copy(remote, self.SRC, self.DST)
        self.assertEqual(remote.timeouts, [storage.COPY_TIMEOUT])
        storage.copy(remote, self.SRC, self.DST, timeout=5.0)
        self.assertEqual(remote.timeouts[-1], 5.0)

    def test_refuses_anything_but_the_sd_card_and_volumes_before_sending_anything(self):
        for path in BAD_COPY_PATHS:
            with self.subTest(f"from {path!r}"):
                remote = FakeRemote(copied())
                with self.assertRaises(LabError):
                    storage.copy(remote, path, self.DST)
                self.assertEqual(remote.commands, [])
            with self.subTest(f"to {path!r}"):
                remote = FakeRemote(copied())
                with self.assertRaises(LabError):
                    storage.copy(remote, self.SRC, path)
                self.assertEqual(remote.commands, [])

    def test_refuses_a_copy_onto_or_into_itself(self):
        for src, dst in (
            (self.SRC, self.SRC),
            ("/tmp/fuse_d/DCIM", "/tmp/fuse_d/DCIM/inner"),
            ("/tmp/fuse_d", "/tmp/fuse_d/x"),
            ("/tmp/quak/mnt/sda1", "/tmp/quak/mnt/sda1/x/y"),
            ("/tmp/fuse_d/a", "/tmp/fuse_d/x/../a/b"),
        ):
            with self.subTest(f"{src} to {dst}"):
                remote = FakeRemote(copied())
                with self.assertRaisesRegex(LabError, "itself"):
                    storage.copy(remote, src, dst)
                self.assertEqual(remote.commands, [])

    def test_a_similar_name_is_not_itself(self):
        result = storage.copy(FakeRemote(copied()), "/tmp/fuse_d/a", "/tmp/fuse_d/ab")
        self.assertEqual(result.target, "/tmp/fuse_d/ab")


class CopyChecksumTests(unittest.TestCase):
    SRC = "/tmp/fuse_d/DCIM/a.mp4"
    DST = "/tmp/quak/mnt/sda1/a.mp4"

    def test_a_match_returns_the_checksum(self):
        self.assertEqual(storage.copy(FakeRemote(copied()), self.SRC, self.DST), CopyResult(self.DST, SHA_A))

    def test_into_a_folder_reports_where_it_went(self):
        result = storage.copy(FakeRemote(copied(into=True)), self.SRC, "/tmp/quak/mnt/sda1/backup")
        self.assertEqual(result, CopyResult("/tmp/quak/mnt/sda1/backup/a.mp4", SHA_A))

    def test_the_scripts_refusal_carries_through(self):
        reply = answer(f"source={SHA_A}\ncopy={SHA_B}\n", exit_code=1, stderr=b"checksums differ\n")
        with self.assertRaisesRegex(LabError, "checksums differ"):
            storage.copy(FakeRemote(reply), self.SRC, self.DST)

    def test_success_is_never_taken_on_the_exit_code_alone(self):
        replies = {
            "differ": copied(other=SHA_B),
            "no output": answer(""),
            "no copy": answer(f"source={SHA_A}\n"),
            "no source": answer(f"copy={SHA_A}\n"),
            "both empty": answer("source=\ncopy=\n"),
            "not hashes": answer("source=sh:\ncopy=sh:\n"),
            "short": answer("source=abc\ncopy=abc\n"),
            "upper case": answer(f"source={SHA_A.upper()}\ncopy={SHA_A.upper()}\n"),
        }
        for name, reply in replies.items():
            with self.subTest(name):
                with self.assertRaisesRegex(LabError, "could not be verified"):
                    storage.copy(FakeRemote(reply), self.SRC, self.DST)

    def test_a_folder_says_it_was_not_verified(self):
        result = storage.copy(FakeRemote(answer("kind=dir\n")), "/tmp/fuse_d/DCIM", "/tmp/quak/mnt/sda1/DCIM")
        self.assertEqual(result, CopyResult("/tmp/quak/mnt/sda1/DCIM", None))

    def test_a_failure_says_why(self):
        reply = answer(exit_code=1, stderr=b"cp: write error: No space left on device\n")
        with self.assertRaisesRegex(LabError, "No space left"):
            storage.copy(FakeRemote(reply), self.SRC, self.DST)

    def test_a_lost_link_raises(self):
        with self.assertRaises(NetworkError):
            storage.copy(FakeRemote(NetworkError("gone")), self.SRC, self.DST)


class NothingDestructiveTests(unittest.TestCase):
    def sent_by_everything(self) -> list[str]:
        remote = FakeRemote(copied())
        storage.list_volumes(remote)
        storage.mount(remote, "sda1")
        storage.mount(remote, "sda1", "usb", read_only=False)
        storage.unmount(remote, "sda1")
        storage.eject(remote, "usb")
        storage.list_dir(remote, "/tmp/fuse_d")
        storage.copy(remote, "/tmp/fuse_d/a", "/tmp/quak/mnt/sda1/a")
        self.assertEqual(len(remote.commands), 7)
        return remote.commands

    def test_there_is_no_function_for_it(self):
        functions = {
            name
            for name, function in inspect.getmembers(storage, inspect.isfunction)
            if function.__module__ == storage.__name__ and not name.startswith("_")
        }
        self.assertEqual(functions, {"list_volumes", "mount", "unmount", "eject", "list_dir", "copy"})

    def test_the_check_would_catch_them(self):
        for command in (
            "mkfs.vfat /dev/sda1",
            "mke2fs /dev/sda1",
            "format /dev/sda",
            "dd if=/dev/zero of=/dev/sda",
            "fdisk /dev/sda",
            "sfdisk /dev/sda",
            "parted /dev/sda",
            "wipefs -a /dev/sda",
        ):
            with self.subTest(command):
                self.assertRegex(command, FORBIDDEN)

    def test_nothing_sent_contains_one(self):
        for command in self.sent_by_everything():
            self.assertNotRegex(command, FORBIDDEN)

    def test_the_only_thing_ever_deleted_is_quaks_own_part_file(self):
        for command in self.sent_by_everything():
            for removal in re.findall(r"\brm\b[^;\n]*", command):
                self.assertEqual(removal, 'rm -f "$part"')

    def test_a_folder_is_only_ever_removed_if_empty(self):
        for command in self.sent_by_everything():
            self.assertNotRegex(command, r"\brm\s+-\S*[rR]")
        self.assertIn("rmdir", "".join(self.sent_by_everything()))

    def test_refuses_each_way_of_aiming_a_copy_at_a_disk(self):
        for device in ("/dev/sda", "/dev/sda1", "/dev/mmcblk0p1", "/tmp/fuse_d/../../dev/sda"):
            with self.subTest(device):
                remote = FakeRemote(copied())
                with self.assertRaisesRegex(LabError, "refusing"):
                    storage.copy(remote, "/tmp/fuse_d/a", device)
                self.assertEqual(remote.commands, [])


class LocalShell:
    """A ``Remote`` that is a shell on this computer, with temporary directories where the camera's paths are."""

    STUBS = r"""
mount() { echo "mount $*" >> "$ROOT/calls"; echo "$3 $4 vfat $2 0 0" >> "$ROOT/mounts"; }
umount() { echo "umount $*" >> "$ROOT/calls"; }
sync() { :; }
blkid() {
  case $1 in
    /dev/sda1) echo '/dev/sda1: LABEL="MY DISK" TYPE="vfat"'; echo '/dev/sdb: LABEL="FORGED" TYPE="forged"' ;;
    /dev/sdb) echo '/dev/sdb: TYPE="ext4"' ;;
    *) return 2 ;;
  esac
}
"""

    def __init__(self, shell: list[str], root: Path) -> None:
        self.shell = shell
        self.root = root
        self.overrides = ""

    def exec(self, command: str, input: bytes | None = None, timeout: float = 120.0) -> ExecResult:
        for camera, local in (
            ("/tmp/fuse_d", "fuse_d"),
            ("/tmp/quak/mnt", "mnt"),
            ("/proc/mounts", "mounts"),
            ("/sys/class/block", "block"),
        ):
            command = command.replace(camera, f"{self.root}/{local}")
        script = f"ROOT={shlex.quote(str(self.root))}\n{self.STUBS}{self.overrides}{command}"
        done = subprocess.run(
            [*self.shell, "-c", script],
            cwd=self.root / "cwd",
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            check=False,
        )
        return ExecResult(exit_code=done.returncode, stdout=done.stdout, stderr=done.stderr)

    def read_file(self, path: str) -> bytes:
        raise AssertionError(f"storage runs commands, but read {path}")

    def write_file(self, path: str, data: bytes) -> None:
        raise AssertionError(f"storage runs commands, but wrote {path}")


class RealShellRuns:
    """The real scripts in a real shell. Subclasses say which shell."""

    shell: list[str]

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.sd = self.root / "fuse_d"
        self.volume = self.root / "mnt" / "sda1"
        self.cwd = self.root / "cwd"
        for folder in (self.sd, self.volume, self.cwd, self.root / "block"):
            folder.mkdir(parents=True)
        self.mounts = self.root / "mounts"
        self.mounts.write_text(f"/dev/sda1 {self.volume} vfat rw 0 0\n", encoding="utf-8")
        self.calls = self.root / "calls"
        self.remote = LocalShell(self.shell, self.root)

    def called(self) -> list[str]:
        return self.calls.read_text(encoding="utf-8").splitlines() if self.calls.exists() else []

    def assert_nothing_ran(self):
        self.assertEqual(list(self.cwd.iterdir()), [], "a name was run as a command")

    def block_device(self, sysfs_path: str, sectors: int) -> None:
        real = self.root / "devices" / sysfs_path
        real.mkdir(parents=True)
        (real / "size").write_text(f"{sectors}\n", encoding="utf-8")
        (self.root / "block" / real.name).symlink_to(real)

    # list_volumes

    def test_list_volumes_reads_a_sysfs_tree(self):
        self.block_device("usb1/block/sda", 2000)
        self.block_device("usb1/block/sda/sda1", 1000)
        self.block_device("usb1/block/sdb", 500)
        self.block_device("usb1/block/sdc", 0)
        self.block_device("virtual/block/loop0", 500)
        self.block_device("virtual/block/ram0", 500)
        self.block_device("virtual/block/zram0", 500)
        self.block_device("virtual/block/mtdblock0", 500)
        self.assertEqual(
            storage.list_volumes(self.remote),
            [
                Volume(
                    device="/dev/sda1",
                    label="MY DISK",
                    fs="vfat",
                    size=1000 * 512,
                    mountpoint=str(self.volume),
                    mounted=True,
                    read_only=False,
                ),
                Volume(device="/dev/sdb", fs="ext4", size=500 * 512),
            ],
        )

    def test_list_volumes_with_no_block_devices_is_quiet(self):
        result = self.remote.exec(storage.LIST_SCRIPT)
        self.assertEqual((result.exit_code, result.stderr), (0, b""))
        self.assertEqual(storage.list_volumes(self.remote), [])

    # mount and unmount

    def test_mount_makes_the_folder_and_reads_back_how_it_went(self):
        volume = storage.mount(self.remote, "sdb1")
        self.assertTrue((self.root / "mnt" / "sdb1").is_dir())
        self.assertEqual(self.called(), [f"mount -o ro,nosuid,nodev,noexec /dev/sdb1 {self.root}/mnt/sdb1"])
        self.assertEqual(
            volume,
            Volume(device="/dev/sdb1", fs="vfat", mountpoint="/tmp/quak/mnt/sdb1", mounted=True, read_only=True),
        )

    def test_a_failed_mount_leaves_no_folder_behind(self):
        self.remote.overrides = 'mount() { echo "mount: bad superblock" >&2; return 1; }\n'
        with self.assertRaisesRegex(LabError, "bad superblock"):
            storage.mount(self.remote, "sdb1")
        self.assertFalse((self.root / "mnt" / "sdb1").exists())

    def test_a_mounted_device_is_not_mounted_again(self):
        with self.assertRaisesRegex(LabError, "already mounted"):
            storage.mount(self.remote, "sda1", "other")
        self.assertEqual(self.called(), [])
        self.assertFalse((self.root / "mnt" / "other").exists())

    def test_a_mountpoint_in_use_is_not_mounted_over(self):
        (self.volume / "file").write_text("still here", encoding="utf-8")
        with self.assertRaisesRegex(LabError, "already mounted"):
            storage.mount(self.remote, "sdb1", "sda1")
        self.assertEqual(self.called(), [])
        self.assertTrue((self.volume / "file").exists())

    def test_unmount_removes_the_empty_folder(self):
        storage.unmount(self.remote, "/tmp/quak/mnt/sda1")
        self.assertEqual(self.called(), [f"umount {self.volume}"])
        self.assertFalse(self.volume.exists())

    def test_unmount_leaves_a_folder_with_something_in_it(self):
        (self.volume / "file").write_text("x", encoding="utf-8")
        storage.eject(self.remote, "sda1")
        self.assertTrue((self.volume / "file").exists())

    def test_a_failed_unmount_says_why_and_keeps_the_folder(self):
        self.remote.overrides = 'umount() { echo "umount: Device busy" >&2; return 1; }\n'
        with self.assertRaisesRegex(LabError, "Device busy"):
            storage.unmount(self.remote, "sda1")
        self.assertTrue(self.volume.exists())

    # list_dir

    def test_list_dir_tells_files_folders_and_links_apart(self):
        (self.sd / "a.mp4").write_bytes(b"12345")
        (self.sd / "empty").write_bytes(b"")
        (self.sd / "DCIM").mkdir()
        (self.sd / ".hidden").write_bytes(b"123")
        (self.sd / "..odd").write_bytes(b"1")
        (self.sd / "link").symlink_to(self.sd / "a.mp4")
        (self.sd / "dangling").symlink_to(self.sd / "nowhere")
        (self.sd / "dirlink").symlink_to(self.sd / "DCIM")
        self.assertEqual(
            sorted(storage.list_dir(self.remote, "/tmp/fuse_d")),
            [
                DirEntry("..odd", "file", 1),
                DirEntry(".hidden", "file", 3),
                DirEntry("DCIM", "dir", 0),
                DirEntry("a.mp4", "file", 5),
                DirEntry("dangling", "link", 0),
                DirEntry("dirlink", "link", 0),
                DirEntry("empty", "file", 0),
                DirEntry("link", "link", 0),
            ],
        )

    def test_list_dir_finds_nothing_in_an_empty_folder(self):
        self.assertEqual(storage.list_dir(self.remote, "/tmp/fuse_d"), [])

    def test_list_dir_of_a_missing_folder_raises(self):
        with self.assertRaises(LabError):
            storage.list_dir(self.remote, "/tmp/fuse_d/nope")

    def test_list_dir_of_a_nasty_path_goes_there(self):
        folder = self.sd / "it's $(touch pwned) `touch pwned`; a b"
        folder.mkdir()
        (folder / "inside").write_bytes(b"12")
        self.assertEqual(storage.list_dir(self.remote, f"/tmp/fuse_d/{folder.name}"), [DirEntry("inside", "file", 2)])
        self.assert_nothing_ran()

    def test_a_file_name_is_only_ever_data(self):
        names = {
            "a b": "a b",
            "it's": "it's",
            "$(touch pwned)": "$(touch pwned)",
            "`touch pwned`": "`touch pwned`",
            "; touch pwned": "; touch pwned",
            "-rf": "-rf",
            "*": "*",
            'q"q': 'q"q',
            "back\\slash": "back\\slash",
            "%s%d": "%s%d",
            "new\nline": "newline",
            "file\t9\tevil": "file9evil",
            "câmera 相机": "câmera 相机",
        }
        for index, name in enumerate(names):
            (self.sd / name).write_bytes(b"x" * index)
        entries = {entry.name: entry for entry in storage.list_dir(self.remote, "/tmp/fuse_d")}
        self.assertEqual(sorted(entries), sorted(names.values()))
        for index, (name, shown) in enumerate(names.items()):
            with self.subTest(name):
                self.assertEqual(entries[shown], DirEntry(shown, "file", index))
        self.assert_nothing_ran()

    # copy

    def test_copy_verifies_and_gives_the_file_its_name(self):
        data = b"video" * 1000
        (self.sd / "a.mp4").write_bytes(data)
        result = storage.copy(self.remote, "/tmp/fuse_d/a.mp4", "/tmp/quak/mnt/sda1/b.mp4")
        self.assertEqual(result, CopyResult("/tmp/quak/mnt/sda1/b.mp4", hashlib.sha256(data).hexdigest()))
        self.assertEqual((self.volume / "b.mp4").read_bytes(), data)
        self.assertEqual([path.name for path in self.volume.iterdir()], ["b.mp4"])

    def test_copy_into_a_folder_keeps_the_name(self):
        (self.volume / "backup").mkdir()
        (self.sd / "a.mp4").write_bytes(b"x")
        result = storage.copy(self.remote, "/tmp/fuse_d/a.mp4", "/tmp/quak/mnt/sda1/backup")
        self.assertEqual(result.target, "/tmp/quak/mnt/sda1/backup/a.mp4")
        self.assertEqual((self.volume / "backup" / "a.mp4").read_bytes(), b"x")

    def test_copy_works_from_a_volume_to_the_card(self):
        (self.volume / "a.mp4").write_bytes(b"x")
        storage.copy(self.remote, "/tmp/quak/mnt/sda1/a.mp4", "/tmp/fuse_d/a.mp4")
        self.assertEqual((self.sd / "a.mp4").read_bytes(), b"x")

    def test_copy_of_nasty_names_gets_there_and_runs_nothing(self):
        for name in ("a b", "it's", "$(touch pwned)", "`touch pwned`", "; touch pwned", "-rf", "*", 'q"q', "back\\slash"):
            with self.subTest(name):
                data = name.encode()
                (self.sd / name).write_bytes(data)
                result = storage.copy(self.remote, f"/tmp/fuse_d/{name}", f"/tmp/quak/mnt/sda1/{name}")
                self.assertEqual(result.sha256, hashlib.sha256(data).hexdigest())
                self.assertEqual((self.volume / name).read_bytes(), data)
        self.assert_nothing_ran()

    def test_copy_of_a_nasty_name_into_a_folder_reports_it(self):
        (self.volume / "backup").mkdir()
        (self.sd / "new\nline.mp4").write_bytes(b"x")
        result = storage.copy(self.remote, "/tmp/fuse_d/new\nline.mp4", "/tmp/quak/mnt/sda1/backup")
        self.assertEqual(result.target, "/tmp/quak/mnt/sda1/backup/new\nline.mp4")
        self.assertTrue((self.volume / "backup" / "new\nline.mp4").exists())

    def test_a_corrupted_copy_is_removed_and_never_named(self):
        (self.sd / "a.mp4").write_bytes(b"the real thing")
        self.remote.overrides = 'cp() { printf corrupt > "$2"; }\n'
        with self.assertRaisesRegex(LabError, "checksums differ"):
            storage.copy(self.remote, "/tmp/fuse_d/a.mp4", "/tmp/quak/mnt/sda1/a.mp4")
        self.assertEqual(list(self.volume.iterdir()), [])
        self.assertEqual((self.sd / "a.mp4").read_bytes(), b"the real thing")

    def test_without_sha256sum_nothing_is_called_verified(self):
        (self.sd / "a.mp4").write_bytes(b"x")
        self.remote.overrides = "sha256sum() { return 127; }\n"
        with self.assertRaisesRegex(LabError, "checksums differ"):
            storage.copy(self.remote, "/tmp/fuse_d/a.mp4", "/tmp/quak/mnt/sda1/a.mp4")
        self.assertEqual(list(self.volume.iterdir()), [])

    def test_a_failed_copy_leaves_no_part_file(self):
        (self.sd / "a.mp4").write_bytes(b"x")
        self.remote.overrides = 'cp() { printf partial > "$2"; echo "cp: No space left" >&2; return 1; }\n'
        with self.assertRaisesRegex(LabError, "No space left"):
            storage.copy(self.remote, "/tmp/fuse_d/a.mp4", "/tmp/quak/mnt/sda1/a.mp4")
        self.assertEqual(list(self.volume.iterdir()), [])

    def test_copy_never_replaces_a_file(self):
        (self.sd / "a.mp4").write_bytes(b"new")
        (self.volume / "a.mp4").write_bytes(b"old")
        with self.assertRaisesRegex(LabError, "already exists"):
            storage.copy(self.remote, "/tmp/fuse_d/a.mp4", "/tmp/quak/mnt/sda1/a.mp4")
        self.assertEqual((self.volume / "a.mp4").read_bytes(), b"old")

    def test_copy_never_replaces_a_file_in_a_folder_it_copies_into(self):
        (self.sd / "a.mp4").write_bytes(b"new")
        (self.volume / "a.mp4").write_bytes(b"old")
        with self.assertRaisesRegex(LabError, "already exists"):
            storage.copy(self.remote, "/tmp/fuse_d/a.mp4", "/tmp/quak/mnt/sda1")
        self.assertEqual((self.volume / "a.mp4").read_bytes(), b"old")

    def test_a_symlink_on_a_stick_is_not_written_through(self):
        victim = self.root / "victim"
        victim.write_bytes(b"keep")
        (self.sd / "a.mp4").write_bytes(b"new")
        (self.volume / "a.mp4").symlink_to(victim)
        (self.volume / "dangling.mp4").symlink_to(self.root / "made-by-the-link")
        with self.assertRaisesRegex(LabError, "already exists"):
            storage.copy(self.remote, "/tmp/fuse_d/a.mp4", "/tmp/quak/mnt/sda1/a.mp4")
        with self.assertRaisesRegex(LabError, "already exists"):
            storage.copy(self.remote, "/tmp/fuse_d/a.mp4", "/tmp/quak/mnt/sda1/dangling.mp4")
        self.assertEqual(victim.read_bytes(), b"keep")
        self.assertFalse((self.root / "made-by-the-link").exists())

    def test_a_part_file_planted_as_a_symlink_is_not_written_through(self):
        victim = self.root / "victim"
        victim.write_bytes(b"keep")
        (self.sd / "a.mp4").write_bytes(b"new")
        (self.volume / "a.mp4.quak-part").symlink_to(victim)
        storage.copy(self.remote, "/tmp/fuse_d/a.mp4", "/tmp/quak/mnt/sda1/a.mp4")
        self.assertEqual(victim.read_bytes(), b"keep")
        self.assertEqual((self.volume / "a.mp4").read_bytes(), b"new")

    def test_a_part_file_left_by_a_lost_link_is_cleared(self):
        (self.sd / "a.mp4").write_bytes(b"new")
        (self.volume / "a.mp4.quak-part").write_bytes(b"half")
        storage.copy(self.remote, "/tmp/fuse_d/a.mp4", "/tmp/quak/mnt/sda1/a.mp4")
        self.assertEqual([path.name for path in self.volume.iterdir()], ["a.mp4"])

    def test_copy_to_a_volume_that_is_not_mounted_never_lands_in_ram(self):
        (self.root / "mnt" / "sdb1").mkdir()
        (self.sd / "a.mp4").write_bytes(b"x")
        for target in ("/tmp/quak/mnt/sdb1/a.mp4", "/tmp/quak/mnt/sdb1", "/tmp/quak/mnt/sdc1"):
            with self.subTest(target):
                with self.assertRaisesRegex(LabError, "is not mounted"):
                    storage.copy(self.remote, "/tmp/fuse_d/a.mp4", target)
        self.assertEqual(list((self.root / "mnt" / "sdb1").iterdir()), [])
        self.assertFalse((self.root / "mnt" / "sdc1").exists())

    def test_copy_makes_no_missing_folders(self):
        (self.sd / "a.mp4").write_bytes(b"x")
        with self.assertRaisesRegex(LabError, "is not a folder"):
            storage.copy(self.remote, "/tmp/fuse_d/a.mp4", "/tmp/quak/mnt/sda1/nope/a.mp4")
        self.assertEqual(list(self.volume.iterdir()), [])

    def test_copy_of_something_missing_says_so(self):
        with self.assertRaisesRegex(LabError, "does not exist"):
            storage.copy(self.remote, "/tmp/fuse_d/nope.mp4", "/tmp/quak/mnt/sda1/a.mp4")
        self.assertEqual(list(self.volume.iterdir()), [])

    def test_copy_of_something_that_is_not_a_file_is_refused(self):
        (self.sd / "pipe").symlink_to("/dev/zero")
        with self.assertRaisesRegex(LabError, "neither a file nor a folder"):
            storage.copy(self.remote, "/tmp/fuse_d/pipe", "/tmp/quak/mnt/sda1/a.mp4")
        self.assertEqual(list(self.volume.iterdir()), [])

    def test_copy_of_a_folder_takes_everything_and_is_not_verified(self):
        (self.sd / "DCIM" / "100GOPRO").mkdir(parents=True)
        (self.sd / "DCIM" / "100GOPRO" / "a b.mp4").write_bytes(b"x")
        result = storage.copy(self.remote, "/tmp/fuse_d/DCIM", "/tmp/quak/mnt/sda1")
        self.assertEqual(result, CopyResult("/tmp/quak/mnt/sda1/DCIM", None))
        self.assertEqual((self.volume / "DCIM" / "100GOPRO" / "a b.mp4").read_bytes(), b"x")

    def test_copy_of_a_folder_never_merges_into_one_already_there(self):
        (self.sd / "DCIM").mkdir()
        (self.volume / "DCIM").mkdir()
        with self.assertRaisesRegex(LabError, "already exists"):
            storage.copy(self.remote, "/tmp/fuse_d/DCIM", "/tmp/quak/mnt/sda1")

    def test_a_failed_folder_copy_says_something_may_be_left(self):
        (self.sd / "DCIM").mkdir()
        self.remote.overrides = "cp() { return 1; }\n"
        with self.assertRaisesRegex(LabError, "may be left with part of the copy"):
            storage.copy(self.remote, "/tmp/fuse_d/DCIM", "/tmp/quak/mnt/sda1/DCIM")


@unittest.skipUnless(shutil.which("sh"), "no sh on this computer")
class PosixShellTests(RealShellRuns, unittest.TestCase):
    shell = ["sh"]


@unittest.skipUnless(shutil.which("busybox"), "no busybox on this computer")
class BusyBoxShellTests(RealShellRuns, unittest.TestCase):
    shell = ["busybox", "sh"]


if __name__ == "__main__":
    unittest.main()
